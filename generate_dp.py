#!/usr/bin/env python3
"""
generate_dp.py
---------------
Moteur complet de generation d'un dossier de Declaration Prealable (DP) a
partir d'un projet OpenSolar vendu. Enchaine automatiquement :

    1. Recuperation du projet OpenSolar (adresse, kWc, client)
    2. Geocodage (API Adresse) -> coordonnees, code INSEE, emprises de carte
    3. Telechargement du cadastre (source Etalab) pour la commune
    4. Extraction vectorielle des panneaux depuis le design OpenSolar
    5. Ajout du projet dans la base partagee "Projets enerev.gpkg"
    6. Duplication du template QGIS avec le bon fid + les bonnes emprises

A la fin : un fichier .qgz pret a etre ouvert dans QGIS pour verification
et export PDF (l'export PDF lui-meme n'est pas automatise dans cette
version -- cf. le README en bas de fichier).

Usage minimal :
    python generate_dp.py --project-id 10349451 --type "Toiture photovoltaïque" \
        --username adrien.piro@enerev.fr --password *** \
        --template "modele_DP_v4.qgz" \
        --work-dir "C:\\Users\\Adrien Piro\\OneDrive - ENEREV\\Outils\\Automation\\Urbanisme" \
        --projets-gpkg "C:\\...\\Projets\\Projets enerev.gpkg"

Pre-requis :
    pip install requests
    ogr2ogr disponible (fourni avec QGIS)
"""

import argparse
import base64
from dataclasses import dataclass
import glob
import gzip
import json
import math
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import zipfile
from pathlib import Path

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:
    print("Le paquet 'requests' est requis : pip install requests", file=sys.stderr)
    sys.exit(1)

OS_API_BASE = "https://api.opensolar.com"
BAN_URL = "https://api-adresse.data.gouv.fr/search/"
CADASTRE_BASE_URL = "https://cadastre.data.gouv.fr/data/etalab-cadastre/latest/geojson/communes"

CADASTRE_LAYERS = {
    "communes": "Communes",
    "sections": "Sections",
    "parcelles": "Parcelles",
    "batiments": "Bati",
}

SCALE_TO_CARTE_ID = {20000: "Carte 1", 5000: "Carte 5", 1000: "Carte 2"}
PLAN_MASSE_SCALE = 250
PLAN_MASSE_CARTE_IDS = ["Carte Masse Avant", "Carte Masse Apres"]
TOITURE_SCALE = 150
TOITURE_CARTE_IDS = ["Carte Toiture Avant", "Carte Toiture Apres"]
COTES_OFFSET_M = 1.5  # decalage des lignes de cote par rapport aux panneaux (partage avec l'annotation)
MAP_WIDTH_MM, MAP_HEIGHT_MM = 210.0, 170.0  # mesure faite dans le template


# ═════════════════════════════════════════════════════════════
# 1. OPENSOLAR : authentification + recuperation projet/systeme
# ═════════════════════════════════════════════════════════════

def make_session():
    session = requests.Session()
    retry = Retry(total=4, backoff_factor=2, status_forcelist=[500, 502, 503, 504], allowed_methods=["GET", "POST"])
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.mount("http://", HTTPAdapter(max_retries=retry))
    return session


def fetch_project_systems(project_id, org_id=None, token=None, username=None, password=None, mfa=None):
    """Recupere la liste des systemes d'un projet SANS lancer tout le
    pipeline -- utilise par l'appli Streamlit pour proposer un menu
    deroulant de selection quand un projet en a plusieurs, avant
    d'appeler run_pipeline() avec le systeme choisi (cf. app.py).
    Renvoie (systems, token, org_id) -- le token/org_id sont reutilisables
    tels quels pour eviter une seconde authentification dans l'appel a
    run_pipeline() qui suit.
    """
    class _Args:
        pass
    args = _Args()
    args.token, args.org_id = token, org_id
    args.username, args.password, args.mfa = username, password, mfa

    session = requests.Session()
    tok, org = get_token_and_org(args, session)
    systems = list_systems(session, org, project_id, tok)
    return systems, tok, org


def get_token_and_org(args, session):
    if args.token:
        org_id = args.org_id or os.environ.get("OPENSOLAR_ORG_ID")
        if not org_id:
            raise RuntimeError("--token fourni sans --org-id.")
        return args.token, org_id

    if os.environ.get("OPENSOLAR_TOKEN"):
        org_id = args.org_id or os.environ.get("OPENSOLAR_ORG_ID")
        if not org_id:
            raise RuntimeError("OPENSOLAR_TOKEN defini sans org_id (--org-id ou OPENSOLAR_ORG_ID).")
        return os.environ["OPENSOLAR_TOKEN"], org_id

    username = args.username or os.environ.get("OPENSOLAR_USERNAME")
    password = args.password or os.environ.get("OPENSOLAR_PASSWORD")
    if not (username and password):
        raise RuntimeError("Aucun moyen d'authentification fourni (--token/--org-id ou --username/--password).")

    payload = {"username": username, "password": password}
    if args.mfa:
        payload["token"] = args.mfa
    r = session.post(f"{OS_API_BASE}/api-token-auth/", json=payload, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"Echec authentification (HTTP {r.status_code}) : {r.text}")
    data = r.json()
    token = data["token"]
    org_id = args.org_id or data.get("org_id")
    if not org_id:
        r2 = session.get(f"{OS_API_BASE}/api/orgs/", headers={"Authorization": f"Bearer {token}"}, timeout=20)
        if r2.status_code != 200:
            raise RuntimeError(f"Echec recuperation des orgs (HTTP {r2.status_code}) : {r2.text[:500]}")
        orgs = r2.json()
        if not orgs:
            raise RuntimeError("Aucune organisation trouvee pour ce compte.")
        org_id = orgs[0]["id"]
    return token, org_id


def get_project_data(session, org_id, project_id, token):
    r = session.get(
        f"{OS_API_BASE}/api/orgs/{org_id}/projects/{project_id}/",
        headers={"Authorization": f"Bearer {token}"}, timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Echec recuperation du projet (HTTP {r.status_code}) : {r.text[:500]}")
    return r.json()


def update_system_image(session, org_id, project_id, token, system_uuid, out_path, rotation_deg=None, width=2400, height=1800):
    """
    Recupere l'image du systeme (calepinage/rendu) directement depuis
    OpenSolar -- endpoint simple et synchrone, deja utilise et eprouve
    dans generate_etude_faisabilite_v17.py. Contrairement au rapport
    d'ombrage, une seule vue fixe (pas de parametre d'angle/direction cote
    OpenSolar) : si 'rotation_deg' est fourni, l'image est pivotee cote
    Python apres telechargement (voir rotate_image_bytes).
    """
    r = session.get(
        f"{OS_API_BASE}/api/orgs/{org_id}/projects/{project_id}/systems/{system_uuid}/image/",
        params={"width": width, "height": height},
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    if r.status_code != 200 or not r.content:
        raise RuntimeError(f"Echec recuperation de l'image du systeme (HTTP {r.status_code}).")
    content = r.content
    if rotation_deg:
        content = rotate_image_bytes(content, rotation_deg)
    out_path.write_bytes(content)
    print(f"  image du systeme recuperee ({len(r.content)} octets){' + pivotee' if rotation_deg else ''}")


def rotate_image_bytes(image_bytes: bytes, mapRotation_deg: float, zoom_factor: float = 1.6) -> bytes:
    """
    Pivote une image raster (JPEG) pour l'aligner comme les cartes DP4
    (angle 'rotation_deg' valide visuellement -- meme sens que mapRotation
    QGIS), puis recadre au centre pour :
      1) eliminer les zones blanches introduites par la rotation (en ne
         gardant qu'un carre garanti entierement dans la zone couverte par
         l'image d'origine, quel que soit l'angle) ;
      2) zoomer davantage sur le centre (les panneaux, puisqu'OpenSolar
         centre le systeme dans son rendu) via 'zoom_factor' (>1 = plus
         serre). Augmente zoom_factor pour se rapprocher encore plus des
         panneaux, diminue-le si le recadrage est trop serre.
    """
    import math
    from io import BytesIO
    from PIL import Image

    img = Image.open(BytesIO(image_bytes))
    w, h = img.size

    rotated = img.rotate(-mapRotation_deg, expand=True, fillcolor="white")

    # plus grand carre garanti sans zone blanche, quel que soit l'angle :
    # cote = plus petite dimension d'origine / racine(2)
    inscribed_side = min(w, h) / math.sqrt(2)
    crop_side = inscribed_side / zoom_factor

    cx, cy = rotated.width / 2, rotated.height / 2
    box = (cx - crop_side / 2, cy - crop_side / 2, cx + crop_side / 2, cy + crop_side / 2)
    cropped = rotated.crop(box)

    buf = BytesIO()
    cropped.convert("RGB").save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def list_systems(session, org_id, project_id, token):
    r = session.get(
        f"{OS_API_BASE}/api/orgs/{org_id}/systems/",
        params={"project": project_id, "fieldset": "list"},
        headers={"Authorization": f"Bearer {token}"}, timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Echec recuperation des systemes (HTTP {r.status_code}) : {r.text[:500]}")
    return r.json()


def choose_system(systems, preselected=None, interactive=False):
    """Meme logique que system_selection.py : auto si un seul systeme,
    sinon preselection (uuid/index).

    'interactive' controle le comportement de repli quand aucune
    preselection ne correspond et qu'il y a plusieurs systemes : True
    (reserve a l'usage CLI direct, cf. main()) demande a l'utilisateur au
    clavier via input(). False (defaut, utilise par run_pipeline/l'appli
    Streamlit) leve une RuntimeError explicite a la place.

    Pourquoi ce changement : input() bloque indefiniment sans le moindre
    message quand il est appele depuis l'appli Streamlit -- pas d'erreur
    visible, juste un pipeline qui semble fige (constate par Adrien en
    local le 08/09/2026 : le prompt attendait une reponse dans la fenetre
    PowerShell, invisible depuis le navigateur). Un projet a plusieurs
    systemes doit desormais produire un message clair, pas un blocage
    silencieux.
    """
    if not systems:
        return None
    if len(systems) == 1:
        return systems[0]
    if preselected is not None:
        match = next((s for s in systems if s.get("uuid") == preselected), None)
        if match:
            return match
        try:
            idx = int(preselected)
            if 0 <= idx < len(systems):
                return systems[idx]
        except (TypeError, ValueError):
            pass

    listing = "\n".join(
        f"  [{i}] {s.get('name') or 'Sans nom'} — {s.get('kw_stc')} kWc"
        f"{' (actuel)' if s.get('is_current') else ''} (uuid={s.get('uuid')})"
        for i, s in enumerate(systems)
    )

    if not interactive:
        raise RuntimeError(
            f"Ce projet a {len(systems)} systemes -- indique lequel utiliser :\n{listing}"
        )

    print(f"\n{len(systems)} systemes disponibles :\n{listing}")
    while True:
        choice = input(f"Choisissez le systeme [0-{len(systems)-1}] : ").strip()
        try:
            idx = int(choice)
            if 0 <= idx < len(systems):
                return systems[idx]
        except ValueError:
            pass
        print("Choix invalide.")


def get_project_author(project) -> str:
    """Nom du vendeur assigne au projet (confirme empiriquement : champ
    OpenSolar 'assigned_salesperson_role_name', simple chaine de caracteres)."""
    return project.get("assigned_salesperson_role_name") or ""


def get_client_info(project):
    """Meme logique que generate_etude_faisabilite_v17.py::get_client_info,
    adaptee aux champs necessaires pour le cartouche DP."""
    is_residential = project.get("is_residential") in (1, "1", True)
    contacts = project.get("contacts_data") or project.get("contacts") or []
    contact = contacts[0] if contacts else {}

    if is_residential:
        nom = f"{contact.get('first_name', '')} {contact.get('family_name', '')}".strip() or "—"
    else:
        nom = project.get("business_name") or "—"

    adresse = ", ".join(filter(None, [
        project.get("address"),
        f"{project.get('zip', '')} {project.get('locality', '')}".strip(),
    ]))

    return {"nom_moa": nom, "adresse_site": adresse or "—"}


# ═════════════════════════════════════════════════════════════
# 2. GEOCODAGE (API Adresse / BAN) + calcul des 4 emprises
# ═════════════════════════════════════════════════════════════

def lambert93_forward(lon_deg: float, lat_deg: float) -> tuple:
    """Projection Lambert-93 (EPSG:2154), formule officielle Lambert Conformal
    Conic 2SP. Validee a <3mm contre l'API Adresse sur un cas reel."""
    a = 6378137.0
    f = 1 / 298.257222101
    e2 = f * (2 - f)
    e = math.sqrt(e2)
    phi0, phi1, phi2 = math.radians(46.5), math.radians(44.0), math.radians(49.0)
    lambda0 = math.radians(3.0)
    FE, FN = 700000.0, 6600000.0

    def m(phi):
        return math.cos(phi) / math.sqrt(1 - e2 * math.sin(phi) ** 2)

    def t(phi):
        return math.tan(math.pi / 4 - phi / 2) / ((1 - e * math.sin(phi)) / (1 + e * math.sin(phi))) ** (e / 2)

    m1, m2 = m(phi1), m(phi2)
    t0, t1, t2 = t(phi0), t(phi1), t(phi2)
    n = (math.log(m1) - math.log(m2)) / (math.log(t1) - math.log(t2))
    F = m1 / (n * t1 ** n)
    rho0 = a * F * t0 ** n

    phi, lam = math.radians(lat_deg), math.radians(lon_deg)
    rho = a * F * t(phi) ** n
    theta = n * (lam - lambda0)
    return FE + rho * math.sin(theta), FN + rho0 - rho * math.cos(theta)


def lambert93_inverse(x: float, y: float) -> tuple:
    """Inverse de lambert93_forward (EPSG:2154 -> WGS84), memes parametres/
    ellipsoide. Utilisee pour reconvertir en lon/lat un point calcule en
    Lambert-93 (ex: centroide du toit) -- notamment pour que le marqueur
    'Projet' affiche sur le plan de situation corresponde exactement a la
    parcelle identifiee (et pas a un point geocode voisin, potentiellement
    sur une parcelle adjacente)."""
    a = 6378137.0
    f = 1 / 298.257222101
    e2 = f * (2 - f)
    e = math.sqrt(e2)
    phi0, phi1, phi2 = math.radians(46.5), math.radians(44.0), math.radians(49.0)
    lambda0 = math.radians(3.0)
    FE, FN = 700000.0, 6600000.0

    def m(phi):
        return math.cos(phi) / math.sqrt(1 - e2 * math.sin(phi) ** 2)

    def t(phi):
        return math.tan(math.pi / 4 - phi / 2) / ((1 - e * math.sin(phi)) / (1 + e * math.sin(phi))) ** (e / 2)

    m1, m2 = m(phi1), m(phi2)
    t0, t1, t2 = t(phi0), t(phi1), t(phi2)
    n = (math.log(m1) - math.log(m2)) / (math.log(t1) - math.log(t2))
    F = m1 / (n * t1 ** n)
    rho0 = a * F * t0 ** n

    dx, dy = x - FE, rho0 - (y - FN)
    rho = math.hypot(dx, dy)
    if n < 0:
        rho = -rho
    gamma = math.atan2(dx, dy)
    lam = lambda0 + gamma / n
    t_ = (rho / (a * F)) ** (1 / n)
    phi = math.pi / 2 - 2 * math.atan(t_)
    for _ in range(6):
        phi = math.pi / 2 - 2 * math.atan(t_ * ((1 - e * math.sin(phi)) / (1 + e * math.sin(phi))) ** (e / 2))
    return math.degrees(lam), math.degrees(phi)


def geocode(session, adresse: str, postcode: str = None) -> dict:
    params = {"q": adresse, "limit": 3}
    if postcode:
        params["postcode"] = postcode
    r = session.get(BAN_URL, params=params, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"L'API Adresse a repondu HTTP {r.status_code}")
    features = r.json().get("features", [])
    if not features:
        raise RuntimeError(f"Aucun resultat de geocodage pour '{adresse}'")
    best = features[0]
    props = best["properties"]
    lon, lat = best["geometry"]["coordinates"]
    if props.get("type") != "housenumber":
        print(f"  ATTENTION : correspondance '{props.get('type')}', pas 'housenumber' — verifie l'adresse.", file=sys.stderr)
    return {
        "label": props.get("label"), "score": props.get("score"), "type": props.get("type"),
        "citycode": props.get("citycode"), "lon": lon, "lat": lat,
        # ajoutes pour le CERFA (T2Q/T2V/T2C/T2L) -- deja renvoyes par la BAN,
        # jetes jusqu'ici :
        "housenumber": props.get("housenumber"),
        "street": props.get("street"),
        "postcode": props.get("postcode"),
        "city": props.get("city"),
    }


def compute_extents(x, y, scales, map_width_mm=MAP_WIDTH_MM, map_height_mm=MAP_HEIGHT_MM):
    extents = {}
    for scale in scales:
        w, h = map_width_mm * scale / 1000.0, map_height_mm * scale / 1000.0
        extents[scale] = (x - w / 2, y - h / 2, x + w / 2, y + h / 2)  # xmin, ymin, xmax, ymax
    return extents


# ═════════════════════════════════════════════════════════════
# 3. CADASTRE (Etalab) -> "Données cadastrales.gpkg"
# ═════════════════════════════════════════════════════════════

def find_ogr2ogr():
    exe = shutil.which("ogr2ogr") or shutil.which("ogr2ogr.exe")
    if exe:
        return exe
    if sys.platform.startswith("win"):
        candidates = []
        for base in (r"C:\Program Files", r"C:\Program Files (x86)", r"C:\OSGeo4W", r"C:\OSGeo4W64"):
            candidates += glob.glob(os.path.join(base, "QGIS*", "bin", "ogr2ogr.exe"))
            candidates += glob.glob(os.path.join(base, "bin", "ogr2ogr.exe"))
        if candidates:
            return sorted(candidates)[-1]
    return None


def dept_from_insee(insee: str) -> str:
    insee = insee.strip().upper()
    if len(insee) == 5 and insee[:2] in ("2A", "2B"):
        return insee[:2]
    if insee[:3] in ("971", "972", "973", "974", "975", "976", "977", "978"):
        return insee[:3]
    return insee[:2]


def update_cadastre(session, ogr2ogr_path, insee, out_gpkg):
    dept = dept_from_insee(insee)
    for source, layer_name in CADASTRE_LAYERS.items():
        url = f"{CADASTRE_BASE_URL}/{dept}/{insee}/cadastre-{insee}-{source}.json.gz"
        r = session.get(url, timeout=60)
        if r.status_code != 200:
            raise RuntimeError(f"Echec telechargement '{source}' pour {insee} (HTTP {r.status_code}) : {url}")
        geojson_bytes = gzip.decompress(r.content)
        tmp_path = out_gpkg.parent / f"_tmp_{source}.geojson"
        tmp_path.write_bytes(geojson_bytes)

        exists = out_gpkg.exists()
        cmd = [ogr2ogr_path]
        if exists:
            cmd += ["-update", "-overwrite"]
        cmd += ["-f", "GPKG", str(out_gpkg), str(tmp_path), "-nln", layer_name, "-nlt", "PROMOTE_TO_MULTI"]
        result = subprocess.run(cmd, capture_output=True, text=True)
        tmp_path.unlink(missing_ok=True)
        if result.returncode != 0:
            raise RuntimeError(f"ogr2ogr a echoue pour '{layer_name}' :\n{result.stderr}")
        print(f"  cadastre : {layer_name} mis a jour")


# ═════════════════════════════════════════════════════════════
# 4. PANNEAUX (design OpenSolar) -> "Panneaux.gpkg"
# ═════════════════════════════════════════════════════════════

def mat_cols(m16):
    return [m16[0:4], m16[4:8], m16[8:12], m16[12:16]]


def mat_mult(A, B):
    result = []
    for j in range(4):
        col = [0.0, 0.0, 0.0, 0.0]
        for i in range(4):
            col[i] = sum(A[k][i] * B[j][k] for k in range(4))
        result.append(col)
    return result


IDENTITY = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]


def find_panels(node, parent_world=IDENTITY, grid_context=None, results=None):
    if results is None:
        results = []
    local = mat_cols(node.get("matrix", [1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,0,1]))
    world = mat_mult(parent_world, local)
    node_type = node.get("type")
    if node_type == "OsModuleGrid":
        ud = node.get("userData", {})
        grid_context = {"grid_uuid": node.get("uuid"), "azimuth": ud.get("azimuth"), "slope": ud.get("slope")}
    elif node_type == "OsModule":
        ud = node.get("userData", {})
        size = ud.get("size") or [1.0, 1.0]
        tx, ty = world[3][0], world[3][1]
        axis_x, axis_y = (world[0][0], world[0][1]), (world[1][0], world[1][1])
        half_w, half_h = size[0] / 2.0, size[1] / 2.0
        corners = []
        for sx, sy in [(-1, -1), (1, -1), (1, 1), (-1, 1)]:
            cx = tx + sx * half_w * axis_x[0] + sy * half_h * axis_y[0]
            cy = ty + sx * half_w * axis_x[1] + sy * half_h * axis_y[1]
            corners.append((cx, cy))
        results.append({
            "panel_uuid": node.get("uuid"), "azimuth": ud.get("azimuth", (grid_context or {}).get("azimuth")),
            "slope": ud.get("slope", (grid_context or {}).get("slope")),
            "grid_uuid": (grid_context or {}).get("grid_uuid"), "corners_local": corners,
        })
    for child in node.get("children", []):
        find_panels(child, world, grid_context, results)
    return results


def update_panels(ogr2ogr_path, design, out_gpkg):
    origin_lon, origin_lat = design["object"]["userData"]["sceneOrigin4326"]
    x0, y0 = lambert93_forward(origin_lon, origin_lat)
    panels = find_panels(design["object"])
    if not panels:
        raise RuntimeError("Aucun panneau (OsModule) trouve dans ce design.")

    features = []
    for p in panels:
        ring = [[x0 + cx, y0 + cy] for cx, cy in p["corners_local"]]
        ring.append(ring[0])
        features.append({
            "type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring]},
            "properties": {"panel_uuid": p["panel_uuid"], "grid_uuid": p["grid_uuid"],
                            "azimuth": p["azimuth"], "slope": p["slope"]},
        })
    geojson = {"type": "FeatureCollection", "features": features}
    tmp_path = out_gpkg.parent / "_tmp_panneaux.geojson"
    tmp_path.write_text(json.dumps(geojson), encoding="utf-8")

    if out_gpkg.exists():
        out_gpkg.unlink()
    cmd = [ogr2ogr_path, "-f", "GPKG", str(out_gpkg), str(tmp_path),
           "-nln", "Panneaux", "-nlt", "PROMOTE_TO_MULTI", "-a_srs", "EPSG:2154"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    tmp_path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(f"ogr2ogr a echoue pour 'Panneaux' :\n{result.stderr}")
    print(f"  panneaux : {len(panels)} panneau(x) ecrit(s)")
    return len(panels)


# ═════════════════════════════════════════════════════════════
# 5B. STREET VIEW (DP7 / DP8) -> 3 photos a noms fixes, ecrasees a chaque generation
#
# Utilise OpenStreetMap (Overpass, gratuit, sans cle) pour connaitre la
# vraie orientation de la rue a cet endroit, independamment de la position
# du point de prise de vue Street View le plus proche.
# ═════════════════════════════════════════════════════════════

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
# Miroirs Overpass publics, essayes dans l'ordre si le principal echoue --
# decouvert le 06/09/2026 (log Streamlit Cloud) : overpass-api.de peut
# refuser la connexion TCP (ConnectionError, pas un simple code HTTP 5xx/429)
# depuis certains environnements d'hebergement, ce qui faisait basculer
# silencieusement TOUS les dossiers sur le repli "position Street View brute"
# (get_heading_fallback_from_panorama) -- lequel ne calcule PAS de
# perpendiculaire a la rue, contrairement a get_street_orientation. C'est la
# cause reelle du defaut d'alignement DP7/DP8 signale par Adrien : le code
# de calcul perpendiculaire n'etait tout simplement jamais execute.
OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
STREETVIEW_IMAGE_URL = "https://maps.googleapis.com/maps/api/streetview"
STREETVIEW_SIZE = "1024x768"
STREETVIEW_FOV = 90
OVERPASS_SEARCH_RADIUS_M = 40


def bearing_degrees(lat1, lon1, lat2, lon2) -> float:
    """Azimut (cap compas, 0-360, Nord=0) du point 1 vers le point 2."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)
    x = math.sin(dlambda) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
    theta = math.atan2(x, y)
    return (math.degrees(theta) + 360) % 360


def _nearest_point_on_segment(lat, lon, lat1, lon1, lat2, lon2):
    """Projette (lat, lon) sur un segment, approximation plane locale (suffisante a l'echelle d'une rue).

    Renvoie aussi `interior` : True si le pied de perpendiculaire tombe
    strictement a l'interieur du segment (0 < t < 1), False s'il est
    ecrete a une extremite. Distinction utile pres d'une cassure de rue
    (deux troncons qui se rejoignent a un angle) : le troncon qui longe
    reellement le terrain a generalement une projection interieure, tandis
    qu'un troncon voisin de l'autre cote de la cassure ne s'en approche
    souvent que par une de ses extremites -- s'y fier en cas d'ex-aequo de
    distance donne le mauvais azimut de rue (cf. courrier d'incompletude
    Osny du 30/06/2026, DPC7/DPC8).
    """
    ref_lat = lat1

    def mx(lo):
        return (lo - lon1) * 111320 * math.cos(math.radians(ref_lat))

    def my(la):
        return (la - lat1) * 110540

    px, py = mx(lon), my(lat)
    bx, by = mx(lon2), my(lat2)
    seg_len2 = bx * bx + by * by
    t_raw = 0.0 if seg_len2 == 0 else (px * bx + py * by) / seg_len2
    t = max(0.0, min(1.0, t_raw))
    interior = 0.0 < t_raw < 1.0
    nx, ny = t * bx, t * by
    dist = math.hypot(px - nx, py - ny)
    n_lon = lon1 + nx / (111320 * math.cos(math.radians(ref_lat)))
    n_lat = lat1 + ny / 110540
    return n_lat, n_lon, dist, interior, t


STREETVIEW_METADATA_URL = "https://maps.googleapis.com/maps/api/streetview/metadata"


def _bearing_smoothed_along_way(geom, seg_index: int, t: float, window_m: float = 10.0) -> float:
    """Calcule l'azimut LOCAL de la rue en lissant sur une fenetre de
    +/- window_m le long de la polyligne complete, plutot que sur les 2
    seuls sommets du segment le plus proche.

    Pourquoi : un sommet OSM proche du point de projection (typiquement la
    ou la rue amorce une legere cassure/courbe) peut a lui seul biaiser
    fortement l'azimut si l'on ne regarde QUE le segment le plus proche --
    l'azimut obtenu penche alors vers la direction de la cassure plutot que
    vers l'orientation generale de la rue devant la propriete (cf. courrier
    d'incompletude Osny du 30/06/2026, DPC7/DPC8 : "on dirait que tu as
    pris l'azimut de la rue un peu plus a droite, car il y a une cassure
    pas loin de l'adresse"). Lisser sur ~20 m de rue de part et d'autre du
    point de projection attenue ce biais tout en restant local.
    """
    ref_lat = geom[0]["lat"]
    ref_lon = geom[0]["lon"]

    def mx(lo):
        return (lo - ref_lon) * 111320 * math.cos(math.radians(ref_lat))

    def my(la):
        return (la - ref_lat) * 110540

    pts_m = [(mx(p["lon"]), my(p["lat"])) for p in geom]
    seg_lens = [math.hypot(pts_m[i + 1][0] - pts_m[i][0], pts_m[i + 1][1] - pts_m[i][1]) for i in range(len(pts_m) - 1)]
    cum = [0.0]
    for L in seg_lens:
        cum.append(cum[-1] + L)
    total = cum[-1]
    proj_pos = cum[seg_index] + t * seg_lens[seg_index]

    def point_at(pos):
        pos = max(0.0, min(total, pos))
        for i, L in enumerate(seg_lens):
            if cum[i] <= pos <= cum[i + 1] + 1e-9:
                tt = 0.0 if L < 1e-9 else (pos - cum[i]) / L
                return (pts_m[i][0] + tt * (pts_m[i + 1][0] - pts_m[i][0]),
                        pts_m[i][1] + tt * (pts_m[i + 1][1] - pts_m[i][1]))
        return pts_m[-1]

    xa, ya = point_at(proj_pos - window_m)
    xb, yb = point_at(proj_pos + window_m)
    if math.hypot(xb - xa, yb - ya) < 1e-6:
        # rue trop courte pour la fenetre demandee -- repli sur le segment brut
        xa, ya = pts_m[seg_index]
        xb, yb = pts_m[seg_index + 1]

    def inv(x, y):
        return (ref_lat + y / 110540, ref_lon + x / (111320 * math.cos(math.radians(ref_lat))))

    lat_a, lon_a = inv(xa, ya)
    lat_b, lon_b = inv(xb, yb)
    return bearing_degrees(lat_a, lon_a, lat_b, lon_b) % 180


def get_street_orientation(lat: float, lon: float) -> dict:
    """
    Trouve le troncon de rue OpenStreetMap le plus proche et calcule :
      - heading_property : azimut perpendiculaire a la rue, vers la propriete (DP7)
    Elargit progressivement le rayon de recherche si rien n'est trouve.
    Essaie plusieurs miroirs Overpass (OVERPASS_MIRRORS) : le principal
    (overpass-api.de) peut refuser la connexion TCP depuis certains
    environnements d'hebergement (constate en production le 06/09/2026),
    ce qui basculait silencieusement sur le repli photo brute -- lequel ne
    calcule pas de perpendiculaire a la rue (cf. get_heading_fallback_from_panorama).
    Leve RuntimeError seulement si TOUS les miroirs echouent -- le code
    appelant doit alors prevoir un repli.
    """
    elements = []
    radius_used = None
    last_error = None
    connectivity_dead = False  # tous les miroirs ont echoue par panne reseau
    # (pas juste "aucune rue a ce rayon") -- inutile de reessayer aux rayons
    # plus larges dans ce cas, l'echec sera identique (constate : sans cette
    # coupure, les memes 3 echecs de connexion se repetaient 3 fois de
    # suite, une fois par rayon, pour rien).

    for radius in (OVERPASS_SEARCH_RADIUS_M, 100, 200):
        if connectivity_dead:
            break
        query = f"[out:json][timeout:15];way(around:{radius},{lat},{lon})[highway];out geom;"
        got_response = False
        any_mirror_reached = False
        for mirror in OVERPASS_MIRRORS:
            r = None
            for attempt in range(2):
                try:
                    r = requests.post(
                        mirror, data={"data": query},
                        headers={"User-Agent": "enerev-dp-tool/1.0 (contact: adrien.piro@enerev.fr)"},
                        timeout=25,
                    )
                except requests.exceptions.RequestException as exc:
                    # panne de connexion (TCP refuse, DNS, timeout...) -- pas
                    # la peine de reessayer LE MEME miroir, on passe au suivant
                    last_error = exc
                    print(f"  [Overpass] {mirror} : echec ({exc})", file=sys.stderr)
                    r = None
                    break
                if r.status_code == 200:
                    break
                if r.status_code == 429:
                    # respecter Retry-After si fourni, sinon attendre plus longtemps
                    # qu'une simple erreur serveur (429 = on nous demande explicitement
                    # de ralentir, pas juste une panne transitoire)
                    wait = int(r.headers.get("Retry-After", 15 * (attempt + 1)))
                    if attempt < 1:
                        time.sleep(wait)
                        continue
                elif r.status_code in (502, 503, 504) and attempt < 1:
                    time.sleep(5 * (attempt + 1))
                    continue
                break
            if r is not None and r.status_code == 200:
                got_response = True
                any_mirror_reached = True
                break
            if r is not None:
                any_mirror_reached = True
                last_error = RuntimeError(f"HTTP {r.status_code}")
                print(f"  [Overpass] {mirror} : echec (HTTP {r.status_code})", file=sys.stderr)
        if not got_response:
            if not any_mirror_reached:
                # panne reseau pure sur TOUS les miroirs -- pas la peine
                # d'elargir le rayon, ca echouera pareil
                connectivity_dead = True
            continue
        elements = r.json().get("elements", [])
        radius_used = radius
        if elements:
            break

    if not elements and last_error is not None and radius_used is None:
        raise RuntimeError(f"Overpass indisponible sur tous les miroirs ({last_error})")
    if not elements:
        raise RuntimeError(f"Aucune rue trouvee via OpenStreetMap, meme jusqu'a {radius_used} m.")
    if radius_used > OVERPASS_SEARCH_RADIUS_M:
        print(f"  (rue trouvee seulement en elargissant la recherche a {radius_used} m)")

    best_interior = None
    best_any = None
    for way in elements:
        geom = way.get("geometry", [])
        for i in range(len(geom) - 1):
            lat1, lon1 = geom[i]["lat"], geom[i]["lon"]
            lat2, lon2 = geom[i + 1]["lat"], geom[i + 1]["lon"]
            n_lat, n_lon, dist, interior, t = _nearest_point_on_segment(lat, lon, lat1, lon1, lat2, lon2)
            candidate = (dist, n_lat, n_lon, geom, i, t)
            if best_any is None or dist < best_any[0]:
                best_any = candidate
            if interior and (best_interior is None or dist < best_interior[0]):
                best_interior = candidate

    # prefere un troncon dont le pied de perpendiculaire tombe a l'interieur
    # du segment (le terrain longe reellement ce troncon) ; ne se rabat sur
    # le plus proche "toutes extremites comprises" que si aucun troncon
    # n'offre de projection interieure a proximite (impasse, terrain en bout
    # de rue...).
    best = best_interior if best_interior is not None else best_any

    dist, n_lat, n_lon, way_geom, seg_index, t = best
    road_bearing = _bearing_smoothed_along_way(way_geom, seg_index, t)

    perp_a, perp_b = (road_bearing + 90) % 360, (road_bearing - 90) % 360
    bearing_to_property = bearing_degrees(n_lat, n_lon, lat, lon)

    def angular_diff(a, b):
        d = abs(a - b) % 360
        return min(d, 360 - d)

    heading_property = perp_a if angular_diff(perp_a, bearing_to_property) < angular_diff(perp_b, bearing_to_property) else perp_b

    return {
        "road_bearing": road_bearing, "heading_property": heading_property,
        "distance_to_road_m": dist, "source": "osm",
        # point de prise de vue (sur la rue, projete au plus pres du terrain) --
        # utilise pour reporter le point ET les angles des prises de vue DPC7/DPC8
        # sur le plan de situation (DP1) et le plan de masse (DP2), conformement
        # a l'article R. 431-10 d) du code de l'urbanisme.
        "cam_lat": n_lat, "cam_lon": n_lon,
    }


def get_heading_fallback_from_panorama(lat: float, lon: float, api_key: str) -> dict:
    """
    Repli si Overpass est indisponible : utilise la position du point de vue
    Street View le plus proche (moins precis niveau perpendicularite reelle
    a la rue, mais garantit que les photos sont quand meme generees).
    Elargit progressivement le rayon de recherche si rien n'est trouve tout
    pres (rues residentielles parfois peu couvertes).
    """
    last_status = None
    for radius in (50, 200, 500, 1000):
        r = requests.get(
            STREETVIEW_METADATA_URL,
            params={"location": f"{lat},{lon}", "radius": radius, "key": api_key},
            timeout=20,
        )
        if r.status_code != 200:
            raise RuntimeError(f"Echec Metadata Street View (HTTP {r.status_code})")
        data = r.json()
        last_status = data.get("status")
        if last_status == "OK":
            pano_lat, pano_lon = data["location"]["lat"], data["location"]["lng"]
            heading_property = bearing_degrees(pano_lat, pano_lon, lat, lon)
            return {
                "road_bearing": None, "heading_property": heading_property,
                "distance_to_road_m": None, "source": "pano",
                "cam_lat": pano_lat, "cam_lon": pano_lon,
            }

    raise RuntimeError(f"Aucune couverture Street View trouvee, meme jusqu'a 1000 m (status={last_status}).")


def estimate_heading_from_building_footprint(ring: list, ref_x: float, ref_y: float):
    """Estime l'azimut 'face a la rue' a partir du plus long cote du
    polygone batiment (repli quand Overpass echoue totalement -- constate
    a plusieurs reprises en production, cf. courriers d'Adrien des
    06-07/09/2026 : les 3 miroirs echouent parfois simultanement).

    Principe : pour un batiment residentiel a peu pres rectangulaire, le
    plus long cote du polygone correspond generalement a la facade
    principale, qui court parallelement a la rue. La perpendiculaire a ce
    cote donne donc une estimation raisonnable de l'azimut recherche.
    Deux perpendiculaires sont possibles (rue devant OU derriere le
    batiment) : on choisit celle qui pointe vers `ref_x, ref_y` (l'adresse
    geocodee, qui tombe generalement du cote rue) depuis le centre du
    batiment.

    Avantage cle : n'utilise QUE des donnees deja telechargees (cadastre
    Etalab, deja necessaire pour la parcelle) -- aucun appel reseau
    supplementaire, donc aucune nouvelle dependance a un service tiers
    dont la fiabilite echappe a ce pipeline (contrairement a Overpass).
    C'est une estimation, pas une mesure : peut se tromper sur un
    batiment tres irregulier ou en L ou le plus long cote ne longe pas la
    rue -- mais reste nettement plus proche d'une perpendiculaire reelle
    qu'un simple cap brut vers une position Street View.
    """
    pts = ring[:-1] if ring[0] == ring[-1] else ring
    m = len(pts)
    best_len2, best_edge = -1.0, None
    for i in range(m):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % m]
        dx, dy = x2 - x1, y2 - y1
        length2 = dx * dx + dy * dy
        if length2 > best_len2:
            best_len2, best_edge = length2, (dx, dy)

    dx, dy = best_edge
    edge_bearing = math.degrees(math.atan2(dx, dy)) % 180  # ligne non orientee
    perp_a, perp_b = (edge_bearing + 90) % 360, (edge_bearing - 90) % 360

    cx = sum(p[0] for p in pts) / m
    cy = sum(p[1] for p in pts) / m
    bearing_to_ref = math.degrees(math.atan2(ref_x - cx, ref_y - cy)) % 360

    def angular_diff(a, b):
        d = abs(a - b) % 360
        return min(d, 360 - d)

    return perp_a if angular_diff(perp_a, bearing_to_ref) < angular_diff(perp_b, bearing_to_ref) else perp_b


def get_heading_fallback_from_building(lat: float, lon: float, cadastre_gpkg: Path, ogr2ogr_path: str) -> dict:
    """Repli 'niveau 2' quand Overpass est totalement indisponible : estime
    l'azimut a partir de l'empreinte du batiment (cadastre Etalab, deja
    telecharge) plutot que de tomber directement sur le repli 'niveau 3'
    (cap brut vers la position Street View, qui ne cherche meme pas a etre
    perpendiculaire a la rue -- cf. get_heading_fallback_from_panorama)."""
    from parcelle_lookup import find_nearest_bati_ring

    x, y = lambert93_forward(lon, lat)
    ring = find_nearest_bati_ring(cadastre_gpkg, ogr2ogr_path, x, y)
    if ring is None or len(ring) < 4:
        raise RuntimeError("Aucun batiment trouve pres du point pour estimer l'azimut.")

    heading_property = estimate_heading_from_building_footprint(ring, x, y)
    return {
        "road_bearing": None, "heading_property": heading_property,
        "distance_to_road_m": None, "source": "empreinte_batiment",
        "cam_lat": lat, "cam_lon": lon,
    }


def fetch_streetview_image(lat, lon, heading, api_key, out_path, size=STREETVIEW_SIZE, fov=STREETVIEW_FOV, pitch=0):
    params = {"size": size, "location": f"{lat},{lon}", "heading": heading, "fov": fov, "pitch": pitch, "key": api_key}
    r = requests.get(STREETVIEW_IMAGE_URL, params=params, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Echec recuperation image Street View (HTTP {r.status_code})")
    out_path.write_bytes(r.content)


STREETVIEW_PHOTO_FILENAMES = ["Photo_rue_dp7.jpg", "Photo_gauche_dp8.jpg", "Photo_droite_dp8.jpg"]


GOOGLE_TILE_API_URL = "https://tile.googleapis.com/v1"


def get_street_orientation_from_google(lat: float, lon: float, api_key: str, radius_m: int = 50) -> dict:
    """Estime l'azimut 'face a la rue' a partir des metadonnees Street View
    de Google lui-meme (Map Tiles API), plutot que d'une source tierce
    (Overpass) ou d'une approximation (empreinte batiment).

    Principe (suggestion d'Adrien, 08/09/2026 -- meilleure source que tout
    ce qui precede) : la reponse de metadonnees d'un panorama contient
    directement :
      - "heading" : le cap du panorama lui-meme (approximativement l'axe
        de la rue, puisque les vehicules Street View roulent le long de
        la chaussee) ;
      - "links" : les panoramas adjacents le long de la meme rue, chacun
        avec le cap exact vers ce panorama voisin -- la source la plus
        precise, deux liens presque opposes (~180° d'ecart) confirmant un
        troncon de rue rectiligne.
    On privilegie les liens (moyenne circulaire des caps mod 180, pour
    ignorer le sens de circulation) ; a defaut, le cap du panorama lui-
    meme. Necessite l'activation de la 'Map Tiles API' dans Google Cloud
    Console (distincte de la Street View Static API deja utilisee) --
    meme cle API, meme projet/facturation.
    """
    session_resp = requests.post(
        f"{GOOGLE_TILE_API_URL}/createSession",
        params={"key": api_key},
        json={"mapType": "streetview", "language": "fr-FR", "region": "FR"},
        timeout=15,
    )
    if session_resp.status_code != 200:
        raise RuntimeError(f"Map Tiles API : creation de session echouee (HTTP {session_resp.status_code}, {session_resp.text[:200]})")
    session_token = session_resp.json().get("session")
    if not session_token:
        raise RuntimeError("Map Tiles API : reponse de session sans jeton 'session'.")

    meta_resp = requests.get(
        f"{GOOGLE_TILE_API_URL}/streetview/metadata",
        params={"session": session_token, "key": api_key, "lat": lat, "lng": lon, "radius": radius_m},
        timeout=15,
    )
    if meta_resp.status_code != 200:
        raise RuntimeError(f"Map Tiles API : metadonnees indisponibles (HTTP {meta_resp.status_code}, {meta_resp.text[:200]})")
    meta = meta_resp.json()
    if "panoId" not in meta:
        raise RuntimeError(f"Map Tiles API : aucun panorama a proximite ({meta})")

    pano_lat, pano_lon = meta["lat"], meta["lng"]
    links = meta.get("links") or []

    if len(links) >= 2:
        # moyenne circulaire des caps mod 180 (une rue n'a pas de "sens" --
        # deux liens opposes a ~180° doivent converger vers le meme axe)
        angles_mod180 = [h["heading"] % 180 for h in links]
        sin_sum = sum(math.sin(math.radians(a * 2)) for a in angles_mod180)
        cos_sum = sum(math.cos(math.radians(a * 2)) for a in angles_mod180)
        road_bearing = (math.degrees(math.atan2(sin_sum, cos_sum)) / 2) % 180
        source_detail = f"{len(links)} liens panorama"
    elif len(links) == 1:
        road_bearing = links[0]["heading"] % 180
        source_detail = "1 lien panorama"
    else:
        road_bearing = meta.get("heading", 0.0) % 180
        source_detail = "cap du panorama (aucun lien disponible)"

    perp_a, perp_b = (road_bearing + 90) % 360, (road_bearing - 90) % 360
    bearing_to_property = bearing_degrees(pano_lat, pano_lon, lat, lon)

    def angular_diff(a, b):
        d = abs(a - b) % 360
        return min(d, 360 - d)

    heading_property = perp_a if angular_diff(perp_a, bearing_to_property) < angular_diff(perp_b, bearing_to_property) else perp_b

    return {
        "road_bearing": road_bearing, "heading_property": heading_property,
        "distance_to_road_m": None, "source": f"google_tiles ({source_detail})",
        "cam_lat": pano_lat, "cam_lon": pano_lon,
    }


def update_streetview_photos(lat, lon, api_key, work_dir: Path, cadastre_gpkg: Path = None, ogr2ogr_path: str = None):
    street = None
    try:
        street = get_street_orientation_from_google(lat, lon, api_key)
    except Exception as exc_google:
        print(f"  Map Tiles API indisponible ({exc_google}) -> repli sur Overpass/OSM", file=sys.stderr)
        try:
            street = get_street_orientation(lat, lon)
        except Exception as exc:
            print(f"  Overpass indisponible ({exc}) -> repli sur l'empreinte du batiment", file=sys.stderr)
            if cadastre_gpkg is not None and ogr2ogr_path is not None:
                try:
                    street = get_heading_fallback_from_building(lat, lon, cadastre_gpkg, ogr2ogr_path)
                except Exception as exc_bati:
                    print(f"  Empreinte du batiment indisponible aussi ({exc_bati}) -> repli sur la position du point de vue Street View", file=sys.stderr)
            if street is None:
                try:
                    street = get_heading_fallback_from_panorama(lat, lon, api_key)
                except Exception as exc2:
                    # aucune des methodes n'a fonctionne : on supprime les
                    # eventuelles photos d'un AUTRE projet plutot que de les laisser
                    # silencieusement en place (mieux vaut un DP7/DP8 vide et visible
                    # a completer, qu'une mauvaise photo qui passe inapercue)
                    removed = []
                    for filename in STREETVIEW_PHOTO_FILENAMES:
                        fp = work_dir / filename
                        if fp.exists():
                            fp.unlink()
                            removed.append(filename)
                    msg = f"Aucune couverture disponible ici, ni via OSM ni via Street View ({exc2})."
                    if removed:
                        msg += f" Anciennes photos supprimees ({', '.join(removed)}) pour eviter de reutiliser celles d'un autre projet."
                    raise RuntimeError(msg)

    heading_property = street["heading_property"]
    heading_left = (heading_property - 90) % 360
    heading_right = (heading_property + 90) % 360

    fetch_streetview_image(lat, lon, heading_property, api_key, work_dir / "Photo_rue_dp7.jpg")
    fetch_streetview_image(lat, lon, heading_left, api_key, work_dir / "Photo_gauche_dp8.jpg")
    fetch_streetview_image(lat, lon, heading_right, api_key, work_dir / "Photo_droite_dp8.jpg")

    source_label = "OSM (rue reelle)" if street["source"] == "osm" else "repli point de vue (Overpass indisponible)"
    print(f"  [{source_label}] azimuts : face={heading_property:.0f}° gauche={heading_left:.0f}° droite={heading_right:.0f}°")

    # renvoye pour permettre a l'appelant de reporter le point ET les angles
    # de prise de vue sur le plan de situation (DP1) et le plan de masse (DP2)
    # -- cf. update_viewpoint_annotation() -- exige par l'art. R. 431-10 d).
    return {
        "cam_lat": street["cam_lat"], "cam_lon": street["cam_lon"],
        "heading_property": heading_property, "heading_left": heading_left, "heading_right": heading_right,
        "source": street["source"],
    }



def update_viewpoint_annotation(ogr2ogr_path, street_info, out_gpkg, arrow_length_m=4.0):
    """
    Reporte le point ET les angles des prises de vue DP7 (paysage proche,
    vue de face) et DP8 (paysage lointain, vues gauche/droite) sur la
    couche "Vues_Texte" (points, avec etiquette et une icone oeil+angle de
    vue qui porte deja visuellement la notion d'angle -- cf.
    templates/dpc_view_icon.svg). Conforme a l'exigence de l'art.
    R. 431-10 d) du code de l'urbanisme (voir courrier d'incompletude Osny
    du 30/06/2026, points DPC1/DPC2).

    Ne dessine PLUS de ligne de visee entre le point de prise de vue et
    chaque icone (supprime a la demande d'Adrien : le "T" forme par ces 3
    lignes convergentes etait visuellement redondant avec l'icone, qui
    porte deja ses propres petites fleches). La couche "Vues" (lignes)
    reste cablee dans le template mais reste vide -- aucune feature n'y
    est plus ecrite.

    L'icone (templates/dpc_view_icon.svg, teinte "enerev teal") est
    referencee par chemin relatif depuis le style de "Vues_Texte" -- elle
    doit donc etre copiee a cote du .qgz genere, comme les .gpkg (voir
    build_project_qgz : les icones .svg du template sont recopiees en
    sibling du .qgz de sortie, sans quoi QGIS ne resout pas le chemin
    relatif et affiche un symbole "manquant" a la place).

    Street View prend les 3 photos depuis un seul et meme point (seul le
    cap/heading change). `arrow_length_m` (reduit a 4 m, contre 8 m
    initialement) ne sert plus qu'a ecarter legerement les 3 icones les
    unes des autres pour eviter qu'elles ne se chevauchent -- une valeur
    plus faible limite aussi la derive visuelle si l'azimut de rue estime
    est legerement imprecis pres d'une cassure (cf. get_street_orientation).

    Chaque feature porte son propre cap ("heading", en degres, 0=Nord) --
    l'icone pivote individuellement pour chaque point de vue (rotation
    data-defined sur le style de "Vues_Texte", expression
    `360 - "heading"` : QGIS fait pivoter les symboles dans le sens
    ANTI-horaire pour un angle positif, l'oppose du cap compas qui
    augmente dans le sens horaire -- d'ou l'inversion).

    Ne genere plus le point/etiquette "Point de prise de vue (DPC7/DPC8)"
    (retire a la demande d'Adrien : redondant avec les 3 icones DP7/DP8
    gauche/DP8 droite, qui portent deja l'information utile).
    """
    cam_lon, cam_lat = street_info["cam_lon"], street_info["cam_lat"]
    camx, camy = lambert93_forward(cam_lon, cam_lat)

    def tip(heading_deg):
        rad = math.radians(heading_deg)
        dx, dy = math.sin(rad), math.cos(rad)  # cap compas (0=Nord) -> vecteur (est, nord)
        return (camx + dx * arrow_length_m, camy + dy * arrow_length_m)

    views = [
        ("DP7", street_info["heading_property"]),
        ("DP8 gauche", street_info["heading_left"]),
        ("DP8 droite", street_info["heading_right"]),
    ]

    line_features, point_features = [], []
    for label, heading in views:
        if heading is None:
            continue
        tx, ty = tip(heading)
        point_features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [tx, ty]},
            "properties": {"label": label, "heading": round(heading, 1)},
        })

    line_geojson = {"type": "FeatureCollection", "features": line_features}
    tmp_line = out_gpkg.parent / "_tmp_vues_ligne.geojson"
    tmp_line.write_text(json.dumps(line_geojson), encoding="utf-8")

    point_geojson = {"type": "FeatureCollection", "features": point_features}
    tmp_point = out_gpkg.parent / "_tmp_vues_texte.geojson"
    tmp_point.write_text(json.dumps(point_geojson), encoding="utf-8")

    if out_gpkg.exists():
        out_gpkg.unlink()

    cmd1 = [ogr2ogr_path, "-f", "GPKG", str(out_gpkg), str(tmp_line),
            "-nln", "Vues", "-nlt", "LINESTRING", "-a_srs", "EPSG:2154"]
    result1 = subprocess.run(cmd1, capture_output=True, text=True)
    tmp_line.unlink(missing_ok=True)
    if result1.returncode != 0:
        raise RuntimeError(f"ogr2ogr a echoue pour 'Vues' :\n{result1.stderr}")

    cmd2 = [ogr2ogr_path, "-update", "-f", "GPKG", str(out_gpkg), str(tmp_point),
            "-nln", "Vues_Texte", "-nlt", "POINT", "-a_srs", "EPSG:2154"]
    result2 = subprocess.run(cmd2, capture_output=True, text=True)
    tmp_point.unlink(missing_ok=True)
    if result2.returncode != 0:
        raise RuntimeError(f"ogr2ogr a echoue pour 'Vues_Texte' :\n{result2.stderr}")

    print(f"  points de vue DPC7/DPC8 reportes (plan de situation + plan de masse)")


CARDINAL_LABELS = [
    "Nord", "Nord-Est", "Est", "Sud-Est", "Sud", "Sud-Ouest", "Ouest", "Nord-Ouest",
]


def azimuth_to_cardinal(azimuth_deg) -> str:
    """Convertit un azimut (cap compas, 0=Nord) en point cardinal francais
    (8 directions) -- utilise pour identifier le versant de toiture dans le
    CERFA et la notice DPC11."""
    if azimuth_deg is None:
        return "non determine"
    idx = int(((azimuth_deg % 360) + 22.5) // 45) % 8
    return CARDINAL_LABELS[idx]


def summarize_roof_grids(design):
    """
    Regroupe les panneaux par tableau/pan de toiture (grid_uuid), pour
    identifier les differents versants de toiture equipes -- necessaire au
    CERFA (description des travaux) et a la notice DPC11 (materiaux et
    modalites d'execution -- indiquer sur quel versant les panneaux sont
    installes, cf. courrier d'incompletude Osny du 30/06/2026).
    """
    panels = find_panels(design["object"])
    groups = {}
    for p in panels:
        key = p.get("grid_uuid") or p["panel_uuid"]
        g = groups.setdefault(key, {"azimuth": p["azimuth"], "slope": p["slope"], "panels": []})
        g["panels"].append(p)
    result = []
    for key, g in groups.items():
        result.append({
            "grid_uuid": key, "azimuth": g["azimuth"], "slope": g["slope"],
            "nb_panneaux": len(g["panels"]), "panels": g["panels"],
            "versant": azimuth_to_cardinal(g["azimuth"]),
        })
    return result


def update_ridge_estimate(ogr2ogr_path, design, out_gpkg, frame_extent, ridge_offset_m=0.5, overhang_m=1.5,
                           text_offset_m=2.3, min_slope_deg=5.0):
    """
    Estime une ligne de faitage par pan de toiture equipe et l'ecrit dans
    les couches "Faitage" (lignes) / "Faitage_Texte" (points, etiquette) --
    reportees uniquement sur le plan de masse (DP2 avant/apres), cf.
    courrier d'incompletude Osny du 30/06/2026, point DPC2 ("materialiser
    le faitage").

    Le design OpenSolar (Raw Data API) ne contient QUE la geometrie des
    panneaux (OsModule/OsModuleGrid), pas la geometrie reelle du pan de
    toiture. Le faitage est donc place juste au-dessus du bord amont (cote
    oppose a l'azimut) du tableau de panneaux, sur toute sa largeur + une
    petite marge -- ce qui correspond a l'installation la plus frequente
    (panneaux montes jusqu'en haut de pente). L'etiquette est placee plus
    loin du bord (text_offset_m > ridge_offset_m) pour ne pas chevaucher
    le polygone des panneaux sur le plan.

    Les pans (quasi-)plats (slope < min_slope_deg) sont ignores : l'azimut
    n'y definit pas une direction de faitage fiable.
    """
    origin_lon, origin_lat = design["object"]["userData"]["sceneOrigin4326"]
    x0, y0 = lambert93_forward(origin_lon, origin_lat)

    fxmin, fymin, fxmax, fymax = frame_extent
    fcx, fcy = (fxmin + fxmax) / 2, (fymin + fymax) / 2
    half_w, half_h = (fxmax - fxmin) / 2 * 0.98, (fymax - fymin) / 2 * 0.98

    def clamp_to_frame(pt):
        x, y = pt
        return (max(fcx - half_w, min(fcx + half_w, x)), max(fcy - half_h, min(fcy + half_h, y)))

    line_features, point_features = [], []
    n_estimated = 0

    for grid in summarize_roof_grids(design):
        azimuth, slope = grid["azimuth"], grid["slope"]
        if azimuth is None or slope is None or slope < min_slope_deg:
            continue

        az_rad = math.radians(azimuth)
        axis_y = (math.sin(az_rad), math.cos(az_rad))     # direction de l'azimut (aval, vers le bas de pente)
        axis_x = (axis_y[1], -axis_y[0])                    # perpendiculaire (le long du faitage)

        pts = [(x0 + cx, y0 + cy) for pnl in grid["panels"] for cx, cy in pnl["corners_local"]]
        us = [px * axis_x[0] + py * axis_x[1] for px, py in pts]
        vs = [px * axis_y[0] + py * axis_y[1] for px, py in pts]
        u_min, u_max, v_min = min(us), max(us), min(vs)
        u_mid = (u_min + u_max) / 2

        def point(u, v):
            return (u * axis_x[0] + v * axis_y[0], u * axis_x[1] + v * axis_y[1])

        p1 = clamp_to_frame(point(u_min - overhang_m, v_min - ridge_offset_m))
        p2 = clamp_to_frame(point(u_max + overhang_m, v_min - ridge_offset_m))
        text_pos = clamp_to_frame(point(u_mid, v_min - text_offset_m))

        label = f"Faîtage estimé (versant {grid['versant']}, pente {slope:.0f}°)"
        line_features.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": [list(p1), list(p2)]},
            "properties": {"label": label, "type": "faitage_estime", "grid_uuid": str(grid["grid_uuid"])},
        })
        point_features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": list(text_pos)},
            "properties": {"label": label},
        })
        n_estimated += 1

    if n_estimated == 0:
        print("  faitage : aucun pan incline (>= 5°) detecte -- couche Faitage laissee vide.")

    line_geojson = {"type": "FeatureCollection", "features": line_features}
    tmp_line = out_gpkg.parent / "_tmp_faitage_ligne.geojson"
    tmp_line.write_text(json.dumps(line_geojson), encoding="utf-8")

    point_geojson = {"type": "FeatureCollection", "features": point_features}
    tmp_point = out_gpkg.parent / "_tmp_faitage_texte.geojson"
    tmp_point.write_text(json.dumps(point_geojson), encoding="utf-8")

    if out_gpkg.exists():
        out_gpkg.unlink()

    cmd1 = [ogr2ogr_path, "-f", "GPKG", str(out_gpkg), str(tmp_line),
            "-nln", "Faitage", "-nlt", "LINESTRING", "-a_srs", "EPSG:2154"]
    result1 = subprocess.run(cmd1, capture_output=True, text=True)
    tmp_line.unlink(missing_ok=True)
    if result1.returncode != 0:
        raise RuntimeError(f"ogr2ogr a echoue pour 'Faitage' :\n{result1.stderr}")

    cmd2 = [ogr2ogr_path, "-update", "-f", "GPKG", str(out_gpkg), str(tmp_point),
            "-nln", "Faitage_Texte", "-nlt", "POINT", "-a_srs", "EPSG:2154"]
    result2 = subprocess.run(cmd2, capture_output=True, text=True)
    tmp_point.unlink(missing_ok=True)
    if result2.returncode != 0:
        raise RuntimeError(f"ogr2ogr a echoue pour 'Faitage_Texte' :\n{result2.stderr}")

    if n_estimated:
        print(f"  faitage : {n_estimated} pan(s) estime(s) a partir de l'azimut/pente des panneaux.")


# Le point du projet est toujours exactement au centre des 4 cartes
# (on centre systematiquement l'emprise dessus), donc un point unique
# suffit : QGIS l'affichera via le symbole (etoile/croix) deja configure
# sur la couche "Projet" du template.
# ═════════════════════════════════════════════════════════════

def update_project_marker(ogr2ogr_path, lon, lat, out_gpkg):
    feature = {
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]}, "properties": {"label": "Projet"}}],
    }
    tmp_path = out_gpkg.parent / "_tmp_projet_marker.geojson"
    tmp_path.write_text(json.dumps(feature), encoding="utf-8")
    if out_gpkg.exists():
        out_gpkg.unlink()
    cmd = [ogr2ogr_path, "-f", "GPKG", str(out_gpkg), str(tmp_path), "-nln", "Projet"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    tmp_path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(f"ogr2ogr a echoue pour 'Projet' :\n{result.stderr}")
    print("  marqueur de localisation mis a jour")


# ═════════════════════════════════════════════════════════════
# 5C. COTES DU CHAMP DE PANNEAUX -> "Cotes_panneaux.gpkg" (DP2 apres)
# ═════════════════════════════════════════════════════════════

def _find_panels_3d(node, parent_world=IDENTITY, results=None):
    """Comme find_panels, mais renvoie les 4 coins en 3D (x,y,z) -- necessaire
    pour calculer les vraies dimensions du champ, non raccourcies par la pente."""
    if results is None:
        results = []
    local = mat_cols(node.get("matrix", [1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,0,1]))
    world = mat_mult(parent_world, local)
    if node.get("type") == "OsModule":
        ud = node.get("userData", {})
        size = ud.get("size") or [1.0, 1.0]
        tx, ty, tz = world[3][0], world[3][1], world[3][2]
        axis_x = (world[0][0], world[0][1], world[0][2])
        axis_y = (world[1][0], world[1][1], world[1][2])
        half_w, half_h = size[0] / 2.0, size[1] / 2.0
        corners = []
        for sx, sy in [(-1, -1), (1, -1), (1, 1), (-1, 1)]:
            corners.append((
                tx + sx * half_w * axis_x[0] + sy * half_h * axis_y[0],
                ty + sx * half_w * axis_x[1] + sy * half_h * axis_y[1],
                tz + sx * half_w * axis_x[2] + sy * half_h * axis_y[2],
            ))
        results.append(corners)
    for child in node.get("children", []):
        _find_panels_3d(child, world, results)
    return results


def update_panel_dimensions(ogr2ogr_path, design, out_gpkg, offset_m=COTES_OFFSET_M):
    """
    Genere 2 lignes de cote (largeur, profondeur) autour du champ de
    panneaux, positionnees selon la vue du dessus (seule chose possible sur
    une carte 2D) mais **etiquetees avec les vraies dimensions physiques du
    champ**, mesurees perpendiculairement au plan des panneaux (donc NON
    raccourcies par l'inclinaison du toit).
    """
    origin_lon, origin_lat = design["object"]["userData"]["sceneOrigin4326"]
    x0, y0 = lambert93_forward(origin_lon, origin_lat)

    panels = find_panels(design["object"])
    if not panels:
        raise RuntimeError("Aucun panneau trouve : impossible de calculer les cotes.")

    def norm(v):
        n = math.hypot(*v)
        return (v[0] / n, v[1] / n) if n else (1.0, 0.0)

    def norm3(v):
        n = math.sqrt(sum(c * c for c in v))
        return tuple(c / n for c in v) if n else (1.0, 0.0, 0.0)

    # --- geometrie 2D (vue du dessus), pour POSITIONNER les lignes sur la carte ---
    c0, c1, c2, c3 = panels[0]["corners_local"]
    axis_x = norm((c1[0] - c0[0], c1[1] - c0[1]))
    axis_y = norm((c3[0] - c0[0], c3[1] - c0[1]))

    all_pts = [(x0 + cx, y0 + cy) for p in panels for cx, cy in p["corners_local"]]
    us = [px * axis_x[0] + py * axis_x[1] for px, py in all_pts]
    vs = [px * axis_y[0] + py * axis_y[1] for px, py in all_pts]
    u_min, u_max = min(us), max(us)
    v_min, v_max = min(vs), max(vs)

    def point(u, v):
        return (u * axis_x[0] + v * axis_y[0], u * axis_x[1] + v * axis_y[1])

    width_line = [point(u_min, v_min - offset_m), point(u_max, v_min - offset_m)]
    depth_line = [point(u_max + offset_m, v_min), point(u_max + offset_m, v_max)]

    # --- geometrie 3D (vraie, perpendiculaire au plan), pour le TEXTE affiche ---
    panels_3d = _find_panels_3d(design["object"])
    p0 = panels_3d[0]

    def sub3(a, b):
        return (a[0] - b[0], a[1] - b[1], a[2] - b[2])

    def dot3(a, b):
        return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]

    axis_x3 = norm3(sub3(p0[1], p0[0]))
    axis_y3 = norm3(sub3(p0[3], p0[0]))
    all_pts_3d = [c for p in panels_3d for c in p]
    us3 = [dot3(p, axis_x3) for p in all_pts_3d]
    vs3 = [dot3(p, axis_y3) for p in all_pts_3d]
    largeur = max(us3) - min(us3)
    profondeur = max(vs3) - min(vs3)

    features = [
        {
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": [list(width_line[0]), list(width_line[1])]},
            "properties": {"label": f"{largeur:.1f} m", "type": "largeur"},
        },
        {
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": [list(depth_line[0]), list(depth_line[1])]},
            "properties": {"label": f"{profondeur:.1f} m", "type": "profondeur"},
        },
    ]
    geojson = {"type": "FeatureCollection", "features": features}
    tmp_path = out_gpkg.parent / "_tmp_cotes.geojson"
    tmp_path.write_text(json.dumps(geojson), encoding="utf-8")

    if out_gpkg.exists():
        out_gpkg.unlink()
    cmd = [ogr2ogr_path, "-f", "GPKG", str(out_gpkg), str(tmp_path),
           "-nln", "Cotes", "-nlt", "LINESTRING", "-a_srs", "EPSG:2154"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    tmp_path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(f"ogr2ogr a echoue pour 'Cotes' :\n{result.stderr}")
    print(f"  cotes (vraies dimensions, perpendiculaires au plan) : largeur {largeur:.1f} m, profondeur {profondeur:.1f} m")


def update_panel_annotation(ogr2ogr_path, design, out_gpkg, dp_type, nb_panneaux, frame_extent,
                             arrow_offset_m=6.0, text_offset_m=9.0, frame_margin_ratio=0.9):
    """
    Genere l'annotation de la page DP2 (apres) sous forme de 2 couches
    distinctes -- pattern standard pour un "callout" fiable :
      - "Annotation" (ligne) : la fleche elle-meme, purement visuelle,
        SANS etiquette. Sa pointe touche exactement le coin du rectangle
        forme par les lignes de cote (meme decalage COTES_OFFSET_M), pas
        le coin brut des panneaux -- pour ne pas sembler "entrer dedans".
      - "Annotation_Texte" (point) : place encore plus loin que la queue
        de la fleche (text_offset_m > arrow_offset_m), pour garantir un
        espace visible entre le texte et la fleche. Porte l'attribut
        'label'. Un point s'etiquette de facon fiable (mode Horizontal),
        quelle que soit la longueur du texte.

    `frame_extent` = (xmin, ymin, xmax, ymax) du cadre de carte "plan de
    masse" (Lambert-93) : la queue de fleche et le point de texte sont
    contraints a rester dans ce cadre (avec une marge de securite
    frame_margin_ratio), pour ne jamais deborder hors de la page peu
    importe la position du batiment dans le cadre.
    """
    origin_lon, origin_lat = design["object"]["userData"]["sceneOrigin4326"]
    x0, y0 = lambert93_forward(origin_lon, origin_lat)

    panels = find_panels(design["object"])
    if not panels:
        raise RuntimeError("Aucun panneau trouve : impossible de positionner l'annotation.")

    def norm(v):
        n = math.hypot(*v)
        return (v[0] / n, v[1] / n) if n else (1.0, 0.0)

    c0, c1, c2, c3 = panels[0]["corners_local"]
    axis_x = norm((c1[0] - c0[0], c1[1] - c0[1]))
    axis_y = norm((c3[0] - c0[0], c3[1] - c0[1]))

    all_pts = [(x0 + cx, y0 + cy) for p in panels for cx, cy in p["corners_local"]]
    us = [px * axis_x[0] + py * axis_x[1] for px, py in all_pts]
    vs = [px * axis_y[0] + py * axis_y[1] for px, py in all_pts]
    u_max, v_min = max(us), min(vs)

    def point(u, v):
        return (u * axis_x[0] + v * axis_y[0], u * axis_x[1] + v * axis_y[1])

    # meme coin que le rectangle des lignes de cote (pas le coin brut des panneaux)
    tip = point(u_max + COTES_OFFSET_M, v_min - COTES_OFFSET_M)
    tail = point(u_max + arrow_offset_m, v_min - arrow_offset_m)
    text_pos = point(u_max + text_offset_m, v_min - text_offset_m)

    # contraindre tail/text_pos a rester dans le cadre reel de la carte,
    # avec une marge de securite, pour ne jamais deborder de la page
    fxmin, fymin, fxmax, fymax = frame_extent
    fcx, fcy = (fxmin + fxmax) / 2, (fymin + fymax) / 2
    half_w = (fxmax - fxmin) / 2 * frame_margin_ratio
    half_h = (fymax - fymin) / 2 * frame_margin_ratio

    def clamp_to_frame(pt):
        x, y = pt
        x = max(fcx - half_w, min(fcx + half_w, x))
        y = max(fcy - half_h, min(fcy + half_h, y))
        return (x, y)

    tail = clamp_to_frame(tail)
    text_pos = clamp_to_frame(text_pos)

    label = f"{dp_type}\n({nb_panneaux} panneaux en surimposition)"

    # --- couche 1 : la fleche (ligne, sans etiquette) ---
    line_feature = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": [list(tail), list(tip)]},
            "properties": {},
        }],
    }
    tmp_line = out_gpkg.parent / "_tmp_annotation_ligne.geojson"
    tmp_line.write_text(json.dumps(line_feature), encoding="utf-8")

    # --- couche 2 : le texte (point, au-dela de la queue de la fleche) ---
    point_feature = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": list(text_pos)},
            "properties": {"label": label},
        }],
    }
    tmp_point = out_gpkg.parent / "_tmp_annotation_texte.geojson"
    tmp_point.write_text(json.dumps(point_feature), encoding="utf-8")

    if out_gpkg.exists():
        out_gpkg.unlink()

    cmd1 = [ogr2ogr_path, "-f", "GPKG", str(out_gpkg), str(tmp_line),
            "-nln", "Annotation", "-nlt", "LINESTRING", "-a_srs", "EPSG:2154"]
    result1 = subprocess.run(cmd1, capture_output=True, text=True)
    tmp_line.unlink(missing_ok=True)
    if result1.returncode != 0:
        raise RuntimeError(f"ogr2ogr a echoue pour 'Annotation' :\n{result1.stderr}")

    cmd2 = [ogr2ogr_path, "-update", "-f", "GPKG", str(out_gpkg), str(tmp_point),
            "-nln", "Annotation_Texte", "-nlt", "POINT", "-a_srs", "EPSG:2154"]
    result2 = subprocess.run(cmd2, capture_output=True, text=True)
    tmp_point.unlink(missing_ok=True)
    if result2.returncode != 0:
        raise RuntimeError(f"ogr2ogr a echoue pour 'Annotation_Texte' :\n{result2.stderr}")

    print(f"  annotation panneaux positionnee (ligne + point texte) : \"{label}\"")


# ═════════════════════════════════════════════════════════════
# 6. CHIRURGIE DU TEMPLATE QGIS (fid + emprises) -> nouveau .qgz
# ═════════════════════════════════════════════════════════════

import re

# correspondance entre le nom de champ utilise dans les anciennes expressions
# get_feature('Projets', ...) et le nom de variable de projet qui le remplace
FIELD_TO_VAR = {
    "Type": "dp_type",
    "Taille (kWc)": "dp_taille_kwc",
    "Adresse site": "dp_adresse_site",
    "MOA raison sociale": "dp_moa_nom",
    "MOA Adresse": "dp_moa_adresse",
}


def replace_cartouche_expressions(qgs_text: str) -> tuple:
    """Remplace chaque attribute(get_feature('Projets','fid',N), 'Champ')
    par la variable de projet correspondante (@dp_xxx). Fonctionne quel que
    soit le fid trouve (il n'a plus aucune importance)."""
    total = 0
    for field, var in FIELD_TO_VAR.items():
        pattern = re.compile(
            r"attribute\(\s*get_feature\('Projets',\s*'fid',\s*\d+\)\s*,\s*'" + re.escape(field) + r"'\s*\)"
        )
        qgs_text, n = pattern.subn(f"@{var}", qgs_text)
        total += n
    return qgs_text, total


# libelle statique de la fleche "Toiture photovoltaique (xx panneaux en
# surimposition)" sur la page DP2 (apres) -- remplace par une expression
# dynamique utilisant @dp_type et @dp_nb_panneaux
SURIMPOSITION_LABEL_PATTERN = re.compile(
    r'labelText="[^"]*panneaux en surimposition\)"'
)
SURIMPOSITION_LABEL_EXPRESSION = (
    'labelText="[% @dp_type || \' (\' || @dp_nb_panneaux || \' panneaux en surimposition)\' %]"'
)


def replace_surimposition_label(qgs_text: str) -> tuple:
    """Remplace le texte fixe de la fleche d'annotation des panneaux (DP2
    apres) par une expression dynamique (type + nombre de panneaux reels)."""
    return SURIMPOSITION_LABEL_PATTERN.subn(SURIMPOSITION_LABEL_EXPRESSION, qgs_text)


def _xml_escape(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def inject_project_variables(qgs_text: str, variables: dict) -> tuple:
    """Ajoute de nouvelles variables de projet (Projet > Proprietes > Variables)
    en les ajoutant a la suite des listes existantes (sans toucher aux
    variables deja presentes, ex: cadastre_commune_layer_id)."""
    names_pattern = re.compile(r'(<variableNames type="QStringList">)(.*?)(</variableNames>)', re.DOTALL)
    values_pattern = re.compile(r'(<variableValues type="QStringList">)(.*?)(</variableValues>)', re.DOTALL)

    new_names = "".join(f"<value>{_xml_escape(k)}</value>" for k in variables)
    new_values = "".join(f"<value>{_xml_escape(v)}</value>" for v in variables.values())

    qgs_text, n1 = names_pattern.subn(lambda m: m.group(1) + m.group(2) + new_names + m.group(3), qgs_text)
    qgs_text, n2 = values_pattern.subn(lambda m: m.group(1) + m.group(2) + new_values + m.group(3), qgs_text)
    if n1 == 0 or n2 == 0:
        raise RuntimeError("Bloc <Variables> introuvable dans le template : structure inattendue.")
    return qgs_text


def _resolve_carte_id(qgs_text: str, carte_id: str) -> str:
    """Retrouve l'id exact d'un LayoutItem dans le template, en tolerant les
    variantes d'accentuation/casse (ex: 'Carte Toiture Apres' vs
    'Carte Toiture Après') qui peuvent apparaitre si l'item a ete renomme ou
    ajuste manuellement dans QGIS a un moment donne. Retourne None si aucune
    variante n'est trouvee.
    """
    candidates = {carte_id, carte_id.strip()}
    accent_pairs = [("Apres", "Après"), ("apres", "après")]
    for a, b in accent_pairs:
        candidates |= {c.replace(a, b) for c in list(candidates) if a in c}
        candidates |= {c.replace(b, a) for c in list(candidates) if b in c}

    for cand in candidates:
        if f'id="{cand}"' in qgs_text:
            return cand
    return None


def replace_extent(qgs_text: str, carte_id: str, xmin, ymin, xmax, ymax) -> tuple:
    resolved_id = _resolve_carte_id(qgs_text, carte_id)
    if resolved_id is None:
        return qgs_text, False
    pattern = re.compile(r'<LayoutItem[^>]*id="' + re.escape(resolved_id) + r'"[^>]*>.*?<Extent[^/]*/>', re.DOTALL)
    m = pattern.search(qgs_text)
    if not m:
        return qgs_text, False
    start, end = m.span()
    extent_start = qgs_text.rfind('<Extent', start, end)
    extent_end = qgs_text.find('/>', extent_start) + 2
    new_extent = f'<Extent ymax="{ymax:.4f}" xmin="{xmin:.4f}" xmax="{xmax:.4f}" ymin="{ymin:.4f}"/>'
    return qgs_text[:extent_start] + new_extent + qgs_text[extent_end:], True


def replace_extent_and_rotation(qgs_text: str, carte_id: str, xmin, ymin, xmax, ymax, rotation_deg: float) -> tuple:
    """Comme replace_extent, mais fixe aussi la rotation de la carte
    (attribut mapRotation de son LayoutItem). Ne suppose aucun ordre
    particulier des attributs dans la balise (mapRotation peut apparaitre
    avant OU apres id selon les cas), et tolere les memes variantes
    d'accentuation de l'id que replace_extent."""
    resolved_id = _resolve_carte_id(qgs_text, carte_id)
    if resolved_id is None:
        return qgs_text, False

    qgs_text, ok = replace_extent(qgs_text, carte_id, xmin, ymin, xmax, ymax)
    if not ok:
        return qgs_text, False

    # localiser la balise ouvrante <LayoutItem ... id="carte_id" ...> en
    # entier (independamment de l'ordre des attributs), puis remplacer
    # mapRotation uniquement a l'interieur de cette portion de texte
    tag_pattern = re.compile(r'<LayoutItem\b[^>]*\bid="' + re.escape(resolved_id) + r'"[^>]*>')
    m = tag_pattern.search(qgs_text)
    if not m:
        return qgs_text, False
    tag_text = m.group()
    new_tag_text, n = re.subn(r'(\bmapRotation=")[^"]*(")', lambda mm: mm.group(1) + f"{rotation_deg:.2f}" + mm.group(2), tag_text)
    if n == 0:
        return qgs_text, False
    start, end = m.span()
    return qgs_text[:start] + new_tag_text + qgs_text[end:], True


def circular_mean_degrees(angles_deg: list) -> float:
    """Moyenne circulaire d'une liste d'azimuts (0-360°) -- une moyenne
    arithmetique classique serait fausse pour des angles a cheval sur 0/360
    (ex: moyenne de 10° et 350° doit donner 0°, pas 180°)."""
    sx = sum(math.sin(math.radians(a)) for a in angles_deg)
    sy = sum(math.cos(math.radians(a)) for a in angles_deg)
    return math.degrees(math.atan2(sx, sy)) % 360


def panel_centroid_lambert93(design) -> tuple:
    """Centroide simple (moyenne des coins) du champ de panneaux, en
    Lambert-93. Utilise pour centrer le plan de masse (DP2) sur les
    panneaux plutot que sur le point d'adresse geocode."""
    origin_lon, origin_lat = design["object"]["userData"]["sceneOrigin4326"]
    x0, y0 = lambert93_forward(origin_lon, origin_lat)
    panels = find_panels(design["object"])
    if not panels:
        raise RuntimeError("Aucun panneau trouve : impossible de centrer le plan de masse.")
    all_pts = [(x0 + cx, y0 + cy) for p in panels for cx, cy in p["corners_local"]]
    return sum(p[0] for p in all_pts) / len(all_pts), sum(p[1] for p in all_pts) / len(all_pts)


def compute_panel_tight_view(design, map_width_mm=210.0, map_height_mm=170.0, margin=1.3):
    """
    Calcule une emprise "zoomee au maximum" sur le champ de panneaux,
    centree sur leur centroide reel (pas sur l'adresse geocodee), avec une
    rotation alignant l'azimut moyen des panneaux face au lecteur (vers le
    haut de la page).

    Si plusieurs tableaux de panneaux ont des azimuts differents, on prend
    leur moyenne circulaire (correcte pour des angles, contrairement a une
    simple moyenne arithmetique).

    Retourne (xmin, ymin, xmax, ymax, rotation_deg) ou rotation_deg est la
    valeur a mettre directement dans l'attribut mapRotation de QGIS.

    Convention verifiee empiriquement dans QGIS (item Carte, champ "Rotation
    de la carte") : pour un azimut moyen de 124.5 deg, la rotation qui met
    les panneaux bien de face (vers le haut de la page) est 55.5 deg, soit
    rotation_deg = (180 - azimuth_avg) % 360.
    """
    origin_lon, origin_lat = design["object"]["userData"]["sceneOrigin4326"]
    x0, y0 = lambert93_forward(origin_lon, origin_lat)

    panels = find_panels(design["object"])
    if not panels:
        raise RuntimeError("Aucun panneau trouve : impossible de calculer la vue serree.")

    azimuths = [p["azimuth"] for p in panels if p.get("azimuth") is not None]
    azimuth_avg = circular_mean_degrees(azimuths) if azimuths else 0.0
    rotation_deg = (180.0 - azimuth_avg) % 360.0

    def norm(v):
        n = math.hypot(*v)
        return (v[0] / n, v[1] / n) if n else (1.0, 0.0)

    # axes derives de l'azimut moyen (pas de l'orientation d'un panneau en
    # particulier), pour rester coherent meme avec plusieurs tableaux :
    # azimut = cap boussole (0=Nord) ; axe X (largeur) = perpendiculaire a
    # l'azimut, axe Y (hauteur/pente) = direction de l'azimut lui-meme
    az_rad = math.radians(azimuth_avg)
    axis_y = (math.sin(az_rad), math.cos(az_rad))    # direction de l'azimut (Est, Nord)
    axis_x = (axis_y[1], -axis_y[0])                  # perpendiculaire

    all_pts = [(x0 + cx, y0 + cy) for p in panels for cx, cy in p["corners_local"]]
    us = [px * axis_x[0] + py * axis_x[1] for px, py in all_pts]
    vs = [px * axis_y[0] + py * axis_y[1] for px, py in all_pts]
    u_min, u_max, v_min, v_max = min(us), max(us), min(vs), max(vs)
    largeur, hauteur = u_max - u_min, v_max - v_min

    cx_u, cx_v = (u_min + u_max) / 2, (v_min + v_max) / 2
    center_x = cx_u * axis_x[0] + cx_v * axis_y[0]
    center_y = cx_u * axis_x[1] + cx_v * axis_y[1]

    # echelle necessaire pour que l'emprise (avec marge) tienne dans le cadre
    scale_w = (largeur * margin) / (map_width_mm / 1000.0)
    scale_h = (hauteur * margin) / (map_height_mm / 1000.0)
    # echelle plancher : pour un petit champ de panneaux (quelques m2), un
    # cadrage strictement ajuste au tableau de panneaux (scale_w/scale_h)
    # peut donner un zoom excessif ou l'on ne voit presque plus le batiment
    # (ex: ~1:56 pour un champ de 9x4m). TOITURE_SCALE (1:150) sert de
    # zoom minimal garanti, quitte a dezoomer davantage si le champ de
    # panneaux est plus grand que ce que 1:150 peut contenir.
    scale = max(scale_w, scale_h, TOITURE_SCALE)

    half_w_m = map_width_mm * scale / 1000.0 / 2
    half_h_m = map_height_mm * scale / 1000.0 / 2

    return (
        center_x - half_w_m, center_y - half_h_m,
        center_x + half_w_m, center_y + half_h_m,
        rotation_deg,
    )


def build_project_qgz(template_qgz: Path, out_qgz: Path, dp_variables: dict, extents_by_scale: dict, toiture_view=None):
    # nom unique (horodatage) plutot qu'un nom fixe : evite de devoir supprimer
    # un dossier existant, ce qui echoue parfois si OneDrive le verrouille
    # momentanement pendant une synchronisation
    import time
    tmp_dir = out_qgz.parent / f"_tmp_qgz_{out_qgz.stem}_{int(time.time()*1000)}"
    tmp_dir.mkdir(parents=True)

    with zipfile.ZipFile(template_qgz, "r") as z:
        z.extractall(tmp_dir)

    qgs_files = list(tmp_dir.glob("*.qgs"))
    if not qgs_files:
        raise RuntimeError("Aucun fichier .qgs trouve dans le template.")
    qgs_path = qgs_files[0]

    qgs_text = qgs_path.read_text(encoding="utf-8")

    qgs_text, n_cartouche = replace_cartouche_expressions(qgs_text)
    print(f"  cartouche : {n_cartouche} expression(s) remplacee(s) par des variables de projet")

    qgs_text, n_surimp = replace_surimposition_label(qgs_text)
    print(f"  libelle 'panneaux en surimposition' : {n_surimp} remplacement(s)")

    qgs_text = inject_project_variables(qgs_text, dp_variables)
    print(f"  variables de projet injectees : {', '.join(dp_variables.keys())}")

    ok_count = 0
    for scale, carte_id in SCALE_TO_CARTE_ID.items():
        xmin, ymin, xmax, ymax = extents_by_scale[scale]
        qgs_text, ok = replace_extent(qgs_text, carte_id, xmin, ymin, xmax, ymax)
        ok_count += int(ok)
        if not ok:
            print(f"  ATTENTION : emprise '{carte_id}' (1:{scale}) non trouvee dans le template.", file=sys.stderr)
    print(f"  emprises DP1 remplacees : {ok_count}/{len(SCALE_TO_CARTE_ID)}")

    xmin, ymin, xmax, ymax = extents_by_scale[PLAN_MASSE_SCALE]
    ok_count_masse = 0
    for carte_id in PLAN_MASSE_CARTE_IDS:
        qgs_text, ok = replace_extent(qgs_text, carte_id, xmin, ymin, xmax, ymax)
        ok_count_masse += int(ok)
        if not ok:
            print(f"  ATTENTION : emprise '{carte_id}' non trouvee dans le template.", file=sys.stderr)
    print(f"  emprises plan de masse remplacees : {ok_count_masse}/{len(PLAN_MASSE_CARTE_IDS)}")

    if toiture_view is not None:
        xmin, ymin, xmax, ymax, rotation_deg = toiture_view
        ok_count_toiture = 0
        for carte_id in TOITURE_CARTE_IDS:
            qgs_text, ok = replace_extent_and_rotation(qgs_text, carte_id, xmin, ymin, xmax, ymax, rotation_deg)
            ok_count_toiture += int(ok)
            if not ok:
                print(f"  ATTENTION : emprise '{carte_id}' non trouvee dans le template.", file=sys.stderr)
        print(f"  emprises plan de toiture (DP4, zoom serre + rotation {rotation_deg:.0f}°) remplacees : {ok_count_toiture}/{len(TOITURE_CARTE_IDS)}")

    qgs_path.write_text(qgs_text, encoding="utf-8")

    if out_qgz.exists():
        try:
            out_qgz.unlink()
        except PermissionError:
            raise RuntimeError(
                f"'{out_qgz.name}' est ouvert dans QGIS (ou verrouille par OneDrive) : "
                "ferme-le avant de relancer, ou choisis un autre nom avec --out."
            )

    with zipfile.ZipFile(out_qgz, "w", zipfile.ZIP_DEFLATED) as z:
        for f in tmp_dir.iterdir():
            z.write(f, arcname=f.name)

    # les ressources SVG (icones de symbole, ex: dpc_view_icon.svg) sont
    # referencees par un chemin RELATIF ("./xxx.svg") dans le style des
    # couches -- QGIS resout ce chemin par rapport a l'emplacement du .qgz
    # SUR DISQUE, pas par rapport a son contenu interne au zip (contrairement
    # a ce qu'on pourrait attendre). Il faut donc aussi en laisser une copie
    # a cote du .qgz genere (meme repertoire que les .gpkg), sans quoi
    # l'icone ne se resout pas et QGIS affiche un symbole "manquant" (point
    # d'interrogation) a la place -- constate en testant avec un vrai QGIS.
    for svg_file in tmp_dir.glob("*.svg"):
        shutil.copy2(svg_file, out_qgz.parent / svg_file.name)

    # nettoyage best-effort : un echec ici (OneDrive, antivirus...) ne doit pas
    # faire perdre le .qgz deja genere avec succes
    try:
        shutil.rmtree(tmp_dir)
    except Exception as exc:
        print(f"  (nettoyage du dossier temporaire ignore : {exc})", file=sys.stderr)


# ═════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════

@dataclass
class PipelineResult:
    qgz: Path            # projet QGIS + fichiers de donnees associes (project_dir)
    cerfa: Path           # CERFA rempli (cerfa_DPC_1_1.pdf), pret pour build_gnau_package()
    notice_dpc11: Path    # notice materiaux/execution (DPC11_notice.pdf), pret pour build_gnau_package()
    project_dir: Path


def run_pipeline(
    project_id, dp_type, template, work_dir,
    org_id=None, token=None, username=None, password=None, mfa=None,
    system=None, moa_adresse=None, postcode=None,
    google_api_key=None, out=None, log=print, interactive=False,
):
    """
    Coeur du pipeline, importable (utilise par le CLI main() ci-dessous et
    par l'application Streamlit). Ne fait aucun sys.exit/parsing d'argv --
    leve une exception en cas d'erreur, et renvoie le Path du .qgz produit.

    'log' est une fonction d'affichage (print par defaut) ; l'app Streamlit
    peut y passer st.write ou un callback qui alimente une barre de
    progression, pour afficher l'avancement dans l'UI plutot qu'en console.
    """
    class _Args:
        pass
    args = _Args()
    args.project_id, args.org_id, args.token = project_id, org_id, token
    args.username, args.password, args.mfa = username, password, mfa
    args.system, args.type = system, dp_type
    args.moa_adresse, args.postcode = moa_adresse, postcode
    args.google_api_key = google_api_key

    session = make_session()
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    ogr2ogr_path = find_ogr2ogr()
    if not ogr2ogr_path:
        raise RuntimeError("ogr2ogr introuvable. Lance ce script depuis un environnement QGIS/OSGeo4W.")

    log("[1/8] Authentification OpenSolar...")
    token, org_id = get_token_and_org(args, session)

    log(f"[2/8] Recuperation du projet {project_id}...")
    project = get_project_data(session, org_id, project_id, token)
    systems = list_systems(session, org_id, project_id, token)
    system_obj = choose_system(systems, preselected=system, interactive=interactive)
    if not system_obj:
        raise RuntimeError("Aucun systeme trouve pour ce projet.")
    kwc = system_obj.get("kw_stc")
    client = get_client_info(project)
    moa_adresse = moa_adresse or client["adresse_site"]
    log(f"  {client['nom_moa']} — {client['adresse_site']} — {kwc} kWc")

    log("[3/8] Geocodage...")
    geo = geocode(session, client["adresse_site"], postcode=postcode)
    log(f"  {geo['label']} (score {geo['score']:.2f}, type={geo['type']}, INSEE={geo['citycode']})")
    x0, y0 = lambert93_forward(geo["lon"], geo["lat"])
    extents = compute_extents(x0, y0, list(SCALE_TO_CARTE_ID.keys()) + [PLAN_MASSE_SCALE])

    # sous-dossier automatique nomme d'apres l'adresse reelle du projet --
    # evite tout risque de placeholder, ET isole chaque requete concurrente
    # (important pour un usage multi-utilisateur type app web)
    safe_name = re.sub(r'[\\/:*?"<>|]', "-", client["adresse_site"])[:80].strip()
    project_dir = work_dir / safe_name
    project_dir.mkdir(parents=True, exist_ok=True)
    log(f"  dossier du projet : {project_dir}")

    log("[4/8] Mise a jour du cadastre...")
    cadastre_gpkg = project_dir / "Données cadastrales.gpkg"
    update_cadastre(session, ogr2ogr_path, geo["citycode"], cadastre_gpkg)

    log("[5/8] Extraction des panneaux + marqueur de localisation...")
    raw_design = project.get("design")
    if not raw_design:
        raise RuntimeError("Champ 'design' absent (Raw Data API Access desactive, ou design non finalise).")
    design = json.loads(gzip.decompress(base64.b64decode(raw_design)).decode("utf-8"))
    nb_panneaux = update_panels(ogr2ogr_path, design, project_dir / "Panneaux.gpkg")

    # DP2 (plan de masse) recentre sur le centroide reel des panneaux --
    # l'adresse geocodee (x0,y0) n'est qu'un point de reference administratif,
    # pas forcement au centre du batiment/toiture.
    cx, cy = panel_centroid_lambert93(design)
    extents[PLAN_MASSE_SCALE] = compute_extents(cx, cy, [PLAN_MASSE_SCALE])[PLAN_MASSE_SCALE]

    # calcule ici (plutot que juste avant build_project_qgz) car rotation_deg
    # sert aussi a orienter l'image DP6 ci-dessous
    toiture_view = compute_panel_tight_view(design)
    rotation_deg = toiture_view[-1]

    log("[6/8] Recherche de la parcelle cadastrale...")
    # Sur le centroide reel des panneaux (Lambert-93), pas sur l'adresse
    # geocodee -- celle-ci pointe l'entree/boite aux lettres et peut tomber
    # sur une parcelle annexe (allee, servitude) au lieu de la parcelle du
    # batiment. Le centroide du toit est beaucoup plus fiable.
    from parcelle_lookup import find_parcelle
    parcelle = find_parcelle(cadastre_gpkg, ogr2ogr_path, cx, cy)
    if parcelle is None:
        raise RuntimeError("Aucune parcelle cadastrale trouvee au centroide du toit -- verifie l'adresse/le design.")
    if not parcelle["certain"]:
        log(f"  ATTENTION : centroide hors de toute parcelle, parcelle la plus proche retenue "
            f"({parcelle['section']} {parcelle['numero']}) -- A VERIFIER MANUELLEMENT.")
    else:
        log(f"  parcelle : section {parcelle['section']}, numero {parcelle['numero']}, "
            f"{parcelle['superficie']} m2")


    # Le marqueur affiche sur le plan doit correspondre au MEME point que
    # celui utilise pour identifier la parcelle (cx, cy = centroide du toit),
    # sinon l'etoile peut visuellement tomber sur une parcelle voisine de
    # celle reellement retenue (ex: adresse geocodee sur AO 726 alors que la
    # parcelle du toit, correctement identifiee, est AO 728) -- signale par
    # Adrien sur le dossier Osny du 30/06/2026.
    marker_lon, marker_lat = lambert93_inverse(cx, cy)
    update_project_marker(ogr2ogr_path, marker_lon, marker_lat, project_dir / "Projet.gpkg")
    update_panel_dimensions(ogr2ogr_path, design, project_dir / "Cotes_panneaux.gpkg")
    update_panel_annotation(ogr2ogr_path, design, project_dir / "Annotation_panneaux.gpkg", dp_type, nb_panneaux, extents[PLAN_MASSE_SCALE])

    # faitage estime (plan de masse) -- suite au courrier d'incompletude Osny
    # du 30/06/2026 (DPC2 : "materialiser le faitage"). ESTIMATION
    # AUTOMATIQUE a partir de l'azimut/pente des panneaux (le design
    # OpenSolar ne contient pas la geometrie reelle du pan de toiture) --
    # a verifier visuellement avant depot.
    try:
        update_ridge_estimate(ogr2ogr_path, design, project_dir / "Faitage.gpkg", extents[PLAN_MASSE_SCALE])
    except Exception as exc:
        log(f"  ATTENTION : estimation du faitage echouee, ignoree ({exc})")
        log("  -> le faitage reste a tracer manuellement dans QGIS pour ce projet.")

    try:
        update_system_image(session, org_id, project_id, token, system_obj.get("uuid"), project_dir / "Rendu_systeme.jpg", rotation_deg=rotation_deg)
    except Exception as exc:
        log(f"  ATTENTION : image du systeme non recuperee, ignoree ({exc})")

    log("[7/8] Photos Street View (DP7/DP8) + points de vue (DP1/DP2)...")
    vues_gpkg = project_dir / "Vues_prises.gpkg"
    if google_api_key:
        try:
            street_info = update_streetview_photos(geo["lat"], geo["lon"], google_api_key, project_dir,
                                                     cadastre_gpkg=cadastre_gpkg, ogr2ogr_path=ogr2ogr_path)
            # points ET angles des prises de vue reportes sur le plan de
            # situation et le plan de masse -- suite au courrier
            # d'incompletude Osny du 30/06/2026 (DPC1/DPC2, art. R. 431-10 d)).
            update_viewpoint_annotation(ogr2ogr_path, street_info, vues_gpkg)
        except Exception as exc:
            log(f"  ATTENTION : etape Street View echouee, ignoree ({exc})")
            log("  -> DP7/DP8 et les points de vue restent a completer manuellement pour ce projet.")
            # ancien Vues_prises.gpkg d'un autre projet : on le supprime plutot
            # que de le laisser en place (meme principe que pour les photos).
            if vues_gpkg.exists():
                vues_gpkg.unlink()
    else:
        log("  ignore (pas de google_api_key fourni)")

    log("[8/8] Duplication du template + generation du CERFA...")
    dp_variables = {
        "dp_type": dp_type,
        "dp_taille_kwc": round(float(kwc), 2) if kwc is not None else "",
        "dp_adresse_site": client["adresse_site"],
        "dp_moa_nom": client["nom_moa"],
        "dp_moa_adresse": moa_adresse,
        "dp_nb_panneaux": nb_panneaux,
        "dp_auteur": get_project_author(project),
    }
    out_qgz = Path(out) if out else project_dir / f"{safe_name}.qgz"
    build_project_qgz(Path(template), out_qgz, dp_variables, extents, toiture_view)

    # versants de toiture equipes (azimut/pente par tableau) -- necessaire a
    # la description CERFA ET a la notice DPC11 (materiaux/execution), cf.
    # courrier d'incompletude Osny du 30/06/2026.
    roof_grids = summarize_roof_grids(design)

    from cerfa_export import ProjectCerfaData, build_cerfa_field_values, fill_cerfa, generate_notice_dpc11
    cerfa_data = ProjectCerfaData(
        nb_panneaux=nb_panneaux,
        puissance_crete_kwc=float(kwc) if kwc is not None else 0.0,
        numero_voie=geo.get("housenumber") or "",
        nom_voie=geo.get("street") or "",
        code_postal=geo.get("postcode") or "",
        localite=geo.get("city") or "",
        section_cadastrale=parcelle["section"],
        numero_cadastral=parcelle["numero"],
        superficie_parcelle_m2=parcelle["superficie"],
        prefixe_cadastral=parcelle["prefixe"],
        roof_grids=roof_grids,
    )
    cerfa_values = build_cerfa_field_values(cerfa_data)
    cerfa_template = Path(template).parent / "cerfa_DPC_1_1.pdf"
    cerfa_pdf = project_dir / "cerfa_DPC_1_1.pdf"
    fill_cerfa(cerfa_template, cerfa_pdf, cerfa_values)

    # DPC11 -- notice materiaux + modalites d'execution (versant de toiture
    # inclus) : piece jusqu'ici absente du dossier -- suite au courrier
    # d'incompletude Osny du 30/06/2026 (art. R. 431-14, R. 431-14-1 et
    # R. 441-8-1 du code de l'urbanisme).
    notice_pdf = project_dir / "DPC11_notice.pdf"
    generate_notice_dpc11(
        notice_pdf, nb_panneaux=nb_panneaux,
        puissance_crete_kwc=float(kwc) if kwc is not None else 0.0,
        roof_grids=roof_grids, adresse_site=client["adresse_site"],
    )

    log(f"\nOK : {out_qgz}")
    return PipelineResult(qgz=out_qgz, cerfa=cerfa_pdf, notice_dpc11=notice_pdf, project_dir=project_dir)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--org-id", default=None)
    parser.add_argument("--token", default=None)
    parser.add_argument("--username", default=None)
    parser.add_argument("--password", default=None)
    parser.add_argument("--mfa", default=None)
    parser.add_argument("--system", default=None, help="uuid ou index du systeme si le projet en a plusieurs")
    parser.add_argument("--type", required=True, help="Ex: 'Toiture photovoltaïque' ou 'Carport photovoltaïque'")
    parser.add_argument("--moa-adresse", default=None, help="Adresse du MOA si differente de l'adresse du site")
    parser.add_argument("--postcode", default=None, help="Code postal (aide au geocodage si adresse ambigue)")
    parser.add_argument("--template", required=True, help="Chemin du template .qgz (ex: modele_DP_v4.qgz)")
    parser.add_argument("--work-dir", required=True, help="Dossier racine des projets (ex: '..\\projets') -- un sous-dossier au nom de l'adresse du projet y est cree automatiquement")
    parser.add_argument("--google-api-key", default=None, help="Cle API Google Maps Platform (Street View Static). Si omise, l'etape Street View est ignoree (DP7/DP8 non mis a jour).")
    parser.add_argument("--out", default=None, help="Chemin du .qgz de sortie (defaut: <work-dir>/<adresse>.qgz)")
    args = parser.parse_args()

    try:
        result = run_pipeline(
            project_id=args.project_id, dp_type=args.type, template=args.template, work_dir=args.work_dir,
            org_id=args.org_id, token=args.token, username=args.username, password=args.password, mfa=args.mfa,
            system=args.system, moa_adresse=args.moa_adresse, postcode=args.postcode,
            google_api_key=args.google_api_key, out=args.out, interactive=True,
        )
    except Exception as exc:
        print(f"\nErreur : {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"CERFA rempli : {result.cerfa}")
    print("Ouvre le .qgz dans QGIS, verifie le rendu, puis exporte le layout en PDF.")


if __name__ == "__main__":
    main()


# ═════════════════════════════════════════════════════════════
# README rapide
# ═════════════════════════════════════════════════════════════
#
# Ce script s'arrete volontairement juste avant l'export PDF : le fichier
# .qgz produit est destine a etre ouvert et verifie dans QGIS avant export,
# pour garder un controle visuel a chaque projet (surtout tant que le
# pipeline n'a pas ete valide sur un grand nombre de cas). L'automatisation
# de l'export PDF lui-meme (via qgis_process, en ligne de commande) pourra
# etre ajoutee dans une prochaine iteration une fois ce moteur valide.
#
# Points a garder en tete :
#   - "Données cadastrales.gpkg", "Panneaux.gpkg" et "Projet.gpkg" sont des
#     fichiers PARTAGES (ecrases a chaque execution) : ne lance pas ce
#     script sur un second projet avant d'avoir fini d'exporter le premier.
#   - Le cartouche est rempli via des variables de projet QGIS (@dp_type,
#     @dp_taille_kwc, @dp_adresse_site, @dp_moa_nom, @dp_moa_adresse),
#     injectees fraiches a chaque generation -- plus besoin de base de
#     donnees "Projets" partagee.
#   - --type n'est pas deduit automatiquement d'OpenSolar (ambigu cote
#     API) : a fournir a la main a chaque lancement.
