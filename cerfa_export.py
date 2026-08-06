"""
cerfa_export.py
----------------
Genere le CERFA 16702*02 (Declaration Prealable) rempli et assemble le zip
"pret a importer" dans un Guichet Numerique des Autorisations d'Urbanisme
(GNAU / OPERIS), a partir des donnees deja produites par generate_dp.py.

Format du zip reverse-engineere a partir de deux dossiers reellement
exportes depuis GNAU (Fontenay-aux-Roses et Verneuil-sur-Seine) :

    cerfa_DPC_1_1.pdf   <- le CERFA rempli (376 champs AcroForm)
    DPC1_1_1.pdf        <- DP1  plan de situation
    DPC2_1_1.pdf        <- DP2  plan de masse
    DPC4_1_1.pdf        <- DP4  plan des facades/toitures
    DPC6_1_1.pdf        <- DP6  insertion dans l'environnement
    DPC7_1_1.pdf        <- DP7  photo environnement proche
    DPC8_1_1.pdf        <- DP8  photo paysage lointain

    (ERREURS_1_1.html n'est qu'un rapport de validation genere PAR GNAU a
    l'export ; il n'est pas necessaire a l'import et n'est donc pas recree.)

Ces 6 PDF de pieces sont exactement ceux que produit deja modele_DP_v4.2.qgz
(un report QGIS independant par DP), donc aucun changement cote QGIS n'est
necessaire : il suffit de les renommer/copier au bon endroit.

IMPORTANT (a valider avec Adrien) :
    Le champ D3 ("Coordonnees du declarant" -> adresse) est ambigu dans les
    deux dossiers analyses : D3C_code/D3L_localite restent fixes a
    "95870"/"Bezons" (siege ENEREV) mais D3N_numero/D3V_voie ont ete
    modifies pour correspondre a l'adresse du terrain (14/Rue Marie et
    Pierre Curie, puis 203/Rue Michel Carre) -> incoherent (code postal de
    Bezons + rue du terrain). C'est tres probablement une erreur de saisie
    manuelle sur les deux dossiers precedents.
    Par defaut, CERFA_STATIC_DEFAULTS ci-dessous fixe D3 a l'adresse reelle
    du siege ENEREV (a completer). Passer `declarant_adresse_terrain=True`
    a fill_cerfa() pour reproduire l'ancien comportement (numero/voie du
    terrain) si c'est en fait le comportement voulu.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

from pypdf import PdfReader, PdfWriter

# ---------------------------------------------------------------------------
# 1. Donnees constantes ENEREV (identiques sur tous les dossiers observes)
# ---------------------------------------------------------------------------

CERFA_STATIC_DEFAULTS: dict[str, str] = {
    # 1.2 Vous etes une personne morale
    "D2D_denomination": "enerev",
    "D2R_raison": "enerev",
    "D2J_type": "SAS",
    "D2S_siret": "93458147100018",
    "D2N_nom": "Piro",
    "D2P_prenom": "Adrien",

    # 2. Coordonnees du declarant (siege ENEREV -- A VERIFIER/COMPLETER)
    "D3C_code": "95870",
    "D3L_localite": "Bezons",
    "D3N_numero": "5",
    "D3V_voie": "Rue Charles Francois Daubigny",
    "D3T_telephone": "0978253500",
    "D5GE1_email": "urbanisme",
    "D5GE2_email": "enerev.fr",
    "D5A_acceptation": "/Oui",   # accepte les notifications electroniques

    # 4.1 Nature des travaux
    "C2ZA1_nouvelle": "/Oui",              # nouvelle construction
    "C2ZR1_destination": "autoconsommation solaire",

    # 3.2 Situation juridique du terrain -> toujours "Non" sauf donnee contraire
    "T3H_CUnon": "/Oui",     # certificat d'urbanisme : non
    "T3L_lotnon": "/Oui",    # lotissement : non
    "T3Q_ZACnon": "/Oui",    # ZAC : non
    "T3R_AFUnon": "/Oui",    # AFU : non
    "T3C_PUPnon": "/Oui",    # PUP : non

    # 5. Legislation connexe -> reponses "Non" standard (a revoir si le
    # terrain est en zone protegee / Natura 2000 / site classe, etc.)
    "P3GD1": "/Oui",
    "P3GF1": "/Oui",
    "P3GG1": "/Oui",
    "P3GH1": "/Oui",
    "P5PB1": "/Oui",

    # 8. Engagement du declarant
    "E1L_lieu": "BEZONS",
    "E1S_signature": "Adrien Piro",

    # Bloc CERFA (non modifiable)
    "N1FCA_formulaire": "DPC",
    "N1NCA_numero": "16702*02",
}


@dataclass
class ProjectCerfaData:
    """Donnees variables d'un projet, deja disponibles a la fin de run_pipeline()."""
    nb_panneaux: int
    puissance_crete_kwc: float
    numero_voie: str            # ex: "14"  (T2Q_numero)
    nom_voie: str               # ex: "Rue Marie et Pierre Curie" (T2V_voie)
    code_postal: str            # ex: "92260"
    localite: str               # ex: "FONTENAY AUX ROSES"
    section_cadastrale: str     # ex: "X"
    numero_cadastral: str       # ex: "158"
    superficie_parcelle_m2: int  # ex: 350
    prefixe_cadastral: str = ""
    superficie_totale_m2: Optional[int] = None  # defaut = superficie_parcelle_m2
    date_signature: Optional[date] = None        # defaut = aujourd'hui


def compute_oplstparcelle(prefixe: str, section: str, numero: str, superficie) -> str:
    """Reconstruit le champ interne OPLSTPARCELLE, ex '¢X¢158¢350¢'.

    Confirme empiriquement sur 2 dossiers : le prefixe n'apparait que s'il
    est non vide ; sinon le format est '¢SECTION¢NUMERO¢SUPERFICIE¢'.
    """
    parts = [p for p in (prefixe, section, str(numero), str(superficie)) if p]
    return "¢" + "¢".join(parts) + "¢"


def build_cerfa_field_values(data: ProjectCerfaData, declarant_adresse_terrain: bool = False) -> dict[str, str]:
    """Construit le dict complet des valeurs a ecrire dans le CERFA."""
    values = dict(CERFA_STATIC_DEFAULTS)

    superficie_totale = data.superficie_totale_m2 or data.superficie_parcelle_m2
    sig_date = data.date_signature or date.today()

    values.update({
        "C2ZD1_description": f"Installation de {data.nb_panneaux} panneaux en surimposition",
        "C2ZP1_crete": str(round(data.puissance_crete_kwc)),

        "T2Q_numero": data.numero_voie,
        "T2V_voie": data.nom_voie,
        "T2C_code": data.code_postal,
        "T2L_localite": data.localite,
        "T2F_prefixe": data.prefixe_cadastral,
        "T2S_section": data.section_cadastrale,
        "T2N_numero": data.numero_cadastral,
        "T2T_superficie": str(data.superficie_parcelle_m2),

        "D5T_total": str(superficie_totale),
        "OPLSTPARCELLE": compute_oplstparcelle(
            data.prefixe_cadastral, data.section_cadastrale,
            data.numero_cadastral, data.superficie_parcelle_m2,
        ),

        "E1D_date": sig_date.strftime("%d%m%Y"),
    })

    if declarant_adresse_terrain:
        # reproduit l'ancien comportement observe (numero/voie = terrain,
        # mais code postal/localite laisses au siege -- incoherent, a
        # n'utiliser que si c'est reellement voulu)
        values["D3N_numero"] = data.numero_voie
        values["D3V_voie"] = data.nom_voie

    return values


# ---------------------------------------------------------------------------
# 2. Remplissage du PDF (AcroForm)
# ---------------------------------------------------------------------------

def fill_cerfa(template_path: Path, output_path: Path, field_values: dict[str, str]) -> None:
    """Ecrit `field_values` dans une copie du CERFA vierge `template_path`.

    Les champs non presents dans `field_values` gardent leur valeur du
    template (vide, ou defaut CERFA comme N1NCA_numero).
    """
    reader = PdfReader(str(template_path))
    writer = PdfWriter()
    writer.append(reader)

    for page in writer.pages:
        writer.update_page_form_field_values(page, field_values, auto_regenerate=False)

    # Rend les valeurs visibles a l'ouverture meme si le lecteur PDF ne
    # regenere pas lui-meme les apparences.
    if writer._root_object.get("/AcroForm") is not None:
        writer.set_need_appearances_writer(True)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "wb") as fh:
        writer.write(fh)


# ---------------------------------------------------------------------------
# 3. Assemblage du zip GNAU
# ---------------------------------------------------------------------------

# Correspondance piece jointe -> nom de fichier attendu par GNAU
# (voir bordereau CERFA : seul DPC1 est obligatoire dans tous les cas ;
# DPC2/4/6/7/8 sont ceux produits par modele_DP_v4.2.qgz)
GNAU_PIECE_FILENAMES = {
    "DP1": "DPC1_1_1.pdf",   # plan de situation (obligatoire)
    "DP2": "DPC2_1_1.pdf",   # plan de masse
    "DP4": "DPC4_1_1.pdf",   # facades / toitures
    "DP6": "DPC6_1_1.pdf",   # insertion environnement
    "DP7": "DPC7_1_1.pdf",   # photo proche
    "DP8": "DPC8_1_1.pdf",   # photo lointain
}
CERFA_FILENAME = "cerfa_DPC_1_1.pdf"


def build_gnau_package(
    output_zip: Path,
    cerfa_pdf_path: Path,
    pieces: dict[str, Path],
) -> Path:
    """Assemble le zip pret a etre importe dans GNAU
    (bouton "Importer le dossier" -> "Import du formulaire et des pieces").

    `pieces` : dict dont les cles sont parmi "DP1","DP2","DP4","DP6","DP7","DP8"
    et les valeurs sont les chemins des PDF deja generes par le pipeline
    QGIS (un par report modele_DP_v4.2.qgz). Les cles absentes sont
    simplement omises du zip (ex: pas de DP4 si le projet ne modifie pas
    les facades/toitures).
    """
    output_zip = Path(output_zip)
    output_zip.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(output_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(cerfa_pdf_path, CERFA_FILENAME)
        for key, src_path in pieces.items():
            if key not in GNAU_PIECE_FILENAMES:
                raise ValueError(f"Piece inconnue: {key!r} (attendu parmi {list(GNAU_PIECE_FILENAMES)})")
            zf.write(src_path, GNAU_PIECE_FILENAMES[key])

    return output_zip
