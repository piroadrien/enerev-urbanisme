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


def update_system_image(session, org_id, project_id, token, system_uuid, out_path, rotation_deg=None, width=1600, height=1200):
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


def rotate_image_bytes(image_bytes: bytes, mapRotation_deg: float) -> bytes:
    """
    Pivote une image raster (JPEG) pour l'aligner comme les cartes DP4 --
    meme angle 'rotation_deg' que compute_panel_tight_view (convention
    mapRotation de QGIS, deja validee empiriquement pour les cartes
    vectorielles).

    ATTENTION -- NON VALIDE VISUELLEMENT sur une image raster : PIL fait
    tourner une image dans le sens ANTI-horaire pour un angle positif,
    alors que la convention mapRotation de QGIS semble etre horaire (a
    confirmer). Hypothese de depart ici : angle PIL = -mapRotation_deg (on
    inverse pour compenser le sens oppose). A verifier visuellement sur le
    premier essai -- si le rendu est a l'envers, inverser le signe
    (utiliser +mapRotation_deg au lieu de -mapRotation_deg).
    """
    from io import BytesIO
    from PIL import Image

    img = Image.open(BytesIO(image_bytes))
    pil_angle = -mapRotation_deg  # hypothese a confirmer (voir docstring)
    rotated = img.rotate(pil_angle, expand=True, fillcolor="white")

    buf = BytesIO()
    rotated.convert("RGB").save(buf, format="JPEG", quality=92)
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


def choose_system(systems, preselected=None):
    """Meme logique que system_selection.py : auto si un seul systeme,
    sinon preselection (uuid/index) ou prompt interactif."""
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
    print(f"\n{len(systems)} systemes disponibles :")
    for i, s in enumerate(systems):
        marker = " (actuel)" if s.get("is_current") else ""
        print(f"  [{i}] {s.get('name') or 'Sans nom'} — {s.get('kw_stc')} kWc{marker}")
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
    """Projette (lat, lon) sur un segment, approximation plane locale (suffisante a l'echelle d'une rue)."""
    ref_lat = lat1

    def mx(lo):
        return (lo - lon1) * 111320 * math.cos(math.radians(ref_lat))

    def my(la):
        return (la - lat1) * 110540

    px, py = mx(lon), my(lat)
    bx, by = mx(lon2), my(lat2)
    seg_len2 = bx * bx + by * by
    t = 0.0 if seg_len2 == 0 else max(0, min(1, (px * bx + py * by) / seg_len2))
    nx, ny = t * bx, t * by
    dist = math.hypot(px - nx, py - ny)
    n_lon = lon1 + nx / (111320 * math.cos(math.radians(ref_lat)))
    n_lat = lat1 + ny / 110540
    return n_lat, n_lon, dist


STREETVIEW_METADATA_URL = "https://maps.googleapis.com/maps/api/streetview/metadata"


def get_street_orientation(lat: float, lon: float) -> dict:
    """
    Trouve le troncon de rue OpenStreetMap le plus proche et calcule :
      - heading_property : azimut perpendiculaire a la rue, vers la propriete (DP7)
    Elargit progressivement le rayon de recherche si rien n'est trouve.
    Leve RuntimeError si Overpass est indisponible (429/5xx persistant) --
    le code appelant doit prevoir un repli (cf. get_heading_fallback_from_panorama).
    """
    elements = []
    radius_used = None
    for radius in (OVERPASS_SEARCH_RADIUS_M, 100, 200):
        query = f"[out:json][timeout:15];way(around:{radius},{lat},{lon})[highway];out geom;"
        r = None
        for attempt in range(3):
            r = requests.post(
                OVERPASS_URL, data={"data": query},
                headers={"User-Agent": "enerev-dp-tool/1.0 (contact: adrien.piro@enerev.fr)"},
                timeout=20,
            )
            if r.status_code == 200:
                break
            if r.status_code == 429:
                # respecter Retry-After si fourni, sinon attendre plus longtemps
                # qu'une simple erreur serveur (429 = on nous demande explicitement
                # de ralentir, pas juste une panne transitoire)
                wait = int(r.headers.get("Retry-After", 20 * (attempt + 1)))
                if attempt < 2:
                    time.sleep(wait)
                    continue
            elif r.status_code in (502, 503, 504) and attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            break
        if r.status_code != 200:
            raise RuntimeError(f"Overpass indisponible (HTTP {r.status_code})")
        elements = r.json().get("elements", [])
        radius_used = radius
        if elements:
            break

    if not elements:
        raise RuntimeError(f"Aucune rue trouvee via OpenStreetMap, meme jusqu'a {radius_used} m.")
    if radius_used > OVERPASS_SEARCH_RADIUS_M:
        print(f"  (rue trouvee seulement en elargissant la recherche a {radius_used} m)")

    best = None
    for way in elements:
        geom = way.get("geometry", [])
        for i in range(len(geom) - 1):
            lat1, lon1 = geom[i]["lat"], geom[i]["lon"]
            lat2, lon2 = geom[i + 1]["lat"], geom[i + 1]["lon"]
            n_lat, n_lon, dist = _nearest_point_on_segment(lat, lon, lat1, lon1, lat2, lon2)
            if best is None or dist < best[0]:
                best = (dist, n_lat, n_lon, lat1, lon1, lat2, lon2)

    dist, n_lat, n_lon, lat1, lon1, lat2, lon2 = best
    road_bearing = bearing_degrees(lat1, lon1, lat2, lon2) % 180

    perp_a, perp_b = (road_bearing + 90) % 360, (road_bearing - 90) % 360
    bearing_to_property = bearing_degrees(n_lat, n_lon, lat, lon)

    def angular_diff(a, b):
        d = abs(a - b) % 360
        return min(d, 360 - d)

    heading_property = perp_a if angular_diff(perp_a, bearing_to_property) < angular_diff(perp_b, bearing_to_property) else perp_b

    return {
        "road_bearing": road_bearing, "heading_property": heading_property,
        "distance_to_road_m": dist, "source": "osm",
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
            return {"road_bearing": None, "heading_property": heading_property, "distance_to_road_m": None, "source": "pano"}

    raise RuntimeError(f"Aucune couverture Street View trouvee, meme jusqu'a 1000 m (status={last_status}).")


def fetch_streetview_image(lat, lon, heading, api_key, out_path, size=STREETVIEW_SIZE, fov=STREETVIEW_FOV, pitch=0):
    params = {"size": size, "location": f"{lat},{lon}", "heading": heading, "fov": fov, "pitch": pitch, "key": api_key}
    r = requests.get(STREETVIEW_IMAGE_URL, params=params, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Echec recuperation image Street View (HTTP {r.status_code})")
    out_path.write_bytes(r.content)


STREETVIEW_PHOTO_FILENAMES = ["Photo_rue_dp7.jpg", "Photo_gauche_dp8.jpg", "Photo_droite_dp8.jpg"]


def update_streetview_photos(lat, lon, api_key, work_dir: Path):
    try:
        street = get_street_orientation(lat, lon)
    except Exception as exc:
        print(f"  Overpass indisponible ({exc}) -> repli sur la position du point de vue Street View", file=sys.stderr)
        try:
            street = get_heading_fallback_from_panorama(lat, lon, api_key)
        except Exception as exc2:
            # aucune des deux methodes n'a fonctionne : on supprime les
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

    # nettoyage best-effort : un echec ici (OneDrive, antivirus...) ne doit pas
    # faire perdre le .qgz deja genere avec succes
    try:
        shutil.rmtree(tmp_dir)
    except Exception as exc:
        print(f"  (nettoyage du dossier temporaire ignore : {exc})", file=sys.stderr)


# ═════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════

def run_pipeline(
    project_id, dp_type, template, work_dir,
    org_id=None, token=None, username=None, password=None, mfa=None,
    system=None, moa_adresse=None, postcode=None,
    google_api_key=None, out=None, log=print,
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

    log("[1/7] Authentification OpenSolar...")
    token, org_id = get_token_and_org(args, session)

    log(f"[2/7] Recuperation du projet {project_id}...")
    project = get_project_data(session, org_id, project_id, token)
    systems = list_systems(session, org_id, project_id, token)
    system_obj = choose_system(systems, preselected=system)
    if not system_obj:
        raise RuntimeError("Aucun systeme trouve pour ce projet.")
    kwc = system_obj.get("kw_stc")
    client = get_client_info(project)
    moa_adresse = moa_adresse or client["adresse_site"]
    log(f"  {client['nom_moa']} — {client['adresse_site']} — {kwc} kWc")

    log("[3/7] Geocodage...")
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

    log("[4/7] Mise a jour du cadastre...")
    update_cadastre(session, ogr2ogr_path, geo["citycode"], project_dir / "Données cadastrales.gpkg")

    log("[5/7] Extraction des panneaux + marqueur de localisation...")
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

    update_project_marker(ogr2ogr_path, geo["lon"], geo["lat"], project_dir / "Projet.gpkg")
    update_panel_dimensions(ogr2ogr_path, design, project_dir / "Cotes_panneaux.gpkg")
    update_panel_annotation(ogr2ogr_path, design, project_dir / "Annotation_panneaux.gpkg", dp_type, nb_panneaux, extents[PLAN_MASSE_SCALE])

    try:
        update_system_image(session, org_id, project_id, token, system_obj.get("uuid"), project_dir / "Rendu_systeme.jpg", rotation_deg=rotation_deg)
    except Exception as exc:
        log(f"  ATTENTION : image du systeme non recuperee, ignoree ({exc})")

    log("[6/7] Photos Street View (DP7/DP8)...")
    if google_api_key:
        try:
            update_streetview_photos(geo["lat"], geo["lon"], google_api_key, project_dir)
        except Exception as exc:
            log(f"  ATTENTION : etape Street View echouee, ignoree ({exc})")
            log("  -> DP7/DP8 restent a completer manuellement pour ce projet (aucune photo generee).")
    else:
        log("  ignore (pas de google_api_key fourni)")

    log("[7/7] Duplication du template...")
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

    log(f"\nOK : {out_qgz}")
    return out_qgz


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
        out_qgz = run_pipeline(
            project_id=args.project_id, dp_type=args.type, template=args.template, work_dir=args.work_dir,
            org_id=args.org_id, token=args.token, username=args.username, password=args.password, mfa=args.mfa,
            system=args.system, moa_adresse=args.moa_adresse, postcode=args.postcode,
            google_api_key=args.google_api_key, out=args.out,
        )
    except Exception as exc:
        print(f"\nErreur : {exc}", file=sys.stderr)
        sys.exit(1)

    print("Ouvre ce fichier dans QGIS, verifie le rendu, puis exporte le layout en PDF.")


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
