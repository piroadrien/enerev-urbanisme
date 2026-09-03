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
    DPC11_1_1.pdf       <- DP11 notice materiaux / modalites d'execution
                                (generee directement par ce module, PAS par
                                QGIS -- cf. generate_notice_dpc11() ; ajoutee
                                suite au courrier d'incompletude Osny du
                                30/06/2026, cette piece etait absente avant)

    (ERREURS_1_1.html n'est qu'un rapport de validation genere PAR GNAU a
    l'export ; il n'est pas necessaire a l'import et n'est donc pas recree.)

Les 6 PDF de plans/photos sont exactement ceux que produit deja
modele_DP_v4.3.qgz (un report QGIS independant par DP), donc aucun
changement cote QGIS n'est necessaire pour ceux-la : il suffit de les
renommer/copier au bon endroit. La notice DPC11 est generee separement
(reportlab), car ce n'est pas un plan/rapport QGIS.

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

import io
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
    # liste de dicts {"nb_panneaux", "versant", "slope", ...} -- un par pan
    # de toiture equipe (cf. generate_dp.summarize_roof_grids). Utilise pour
    # detailler le(s) versant(s) dans la description CERFA (C2ZD1) --
    # suite au courrier d'incompletude Osny du 30/06/2026, dont la
    # description generique ("installation de N panneaux en
    # surimposition") a ete jugee insuffisante.
    roof_grids: Optional[list] = None


def compute_oplstparcelle(prefixe: str, section: str, numero: str, superficie) -> str:
    """Reconstruit le champ interne OPLSTPARCELLE, ex '¢X¢158¢350¢'.

    Confirme empiriquement sur 2 dossiers : le prefixe n'apparait que s'il
    est non vide ; sinon le format est '¢SECTION¢NUMERO¢SUPERFICIE¢'.
    """
    parts = [p for p in (prefixe, section, str(numero), str(superficie)) if p]
    return "¢" + "¢".join(parts) + "¢"


def describe_versants(roof_grids: Optional[list]) -> str:
    """Construit le segment de phrase decrivant le(s) versant(s) de toiture
    equipe(s), a partir de generate_dp.summarize_roof_grids(). Vide si
    l'information n'est pas disponible (ne bloque jamais la generation du
    CERFA -- au pire, la description reste generique comme avant)."""
    if not roof_grids:
        return ""
    parts = []
    for g in roof_grids:
        versant = g.get("versant")
        slope = g.get("slope")
        n = g.get("nb_panneaux")
        if not versant or versant == "non determine":
            continue
        seg = f"versant {versant}"
        if slope is not None:
            seg += f" (pente {slope:.0f}°)"
        if n:
            seg += f" : {n} panneau(x)"
        parts.append(seg)
    if not parts:
        return ""
    return ", ".join(parts)


def build_cerfa_field_values(data: ProjectCerfaData, declarant_adresse_terrain: bool = False) -> dict[str, str]:
    """Construit le dict complet des valeurs a ecrire dans le CERFA."""
    values = dict(CERFA_STATIC_DEFAULTS)

    superficie_totale = data.superficie_totale_m2 or data.superficie_parcelle_m2
    sig_date = data.date_signature or date.today()

    versants_desc = describe_versants(data.roof_grids)
    description = f"Installation de {data.nb_panneaux} panneaux photovoltaïques en surimposition sur toiture existante"
    if versants_desc:
        description += f", {versants_desc}"
    description += "."

    values.update({
        "C2ZD1_description": description,
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
# 1bis. Notice DPC11 (materiaux + modalites d'execution)
# ---------------------------------------------------------------------------
#
# Piece jusqu'ici absente du dossier -- suite au courrier d'incompletude
# Osny du 30/06/2026 : "notice faisant apparaitre les materiaux utilises et
# les modalites d'execution des travaux [Art. R. 431-14, R. 431-14-1 et
# R. 441-8-1 du code de l'urbanisme] ... indiquer sur quel versant de la
# toiture les panneaux seront installes".
#
# Le texte des materiaux/modalites ci-dessous est une description standard
# pour une pose ENEREV en surimposition -- A RELIRE/AJUSTER avec Adrien si
# elle ne correspond pas exactement au procede reellement mis en oeuvre
# (marque des rails, fixations, onduleur vs micro-onduleurs, etc.).

NOTICE_MATERIAUX_TEXTE = (
    "Les panneaux photovoltaïques sont installés en surimposition de la couverture existante, "
    "sans modification de la charpente ni de la structure du bâtiment. Le procédé de pose repose "
    "sur des rails de fixation en aluminium anodisé, fixés à la charpente au moyen de crochets ou "
    "pattes de fixation traversant la couverture au droit des chevrons, avec reprise d'étanchéité "
    "à chaque point de fixation. Les modules photovoltaïques sont ensuite clipsés sur les rails au "
    "moyen de pinces (clamps) intermédiaires et d'extrémité, sans perçage des modules. "
    "L'ensemble est raccordé à un ou plusieurs onduleurs/micro-onduleurs, installés en toiture ou en "
    "sous-face, puis au tableau électrique existant."
)

NOTICE_MODALITES_TEXTE = (
    "Les travaux consistent en la pose des rails et fixations, la mise en place des modules, le "
    "câblage électrique (courant continu en toiture, courant alternatif jusqu'au tableau) et les "
    "raccordements. Aucune reprise de couverture n'est nécessaire en dehors des points de fixation, "
    "qui font l'objet d'une reprise d'étanchéité soignée (about, plaque de répartition ou solin selon "
    "le type de couverture). Le chantier n'entraîne pas de modification de l'aspect extérieur du "
    "bâtiment autre que l'ajout des modules eux-mêmes, posés dans le plan de la toiture."
)


def _format_versants_lines(roof_grids: Optional[list]) -> list[str]:
    if not roof_grids:
        return ["Versant(s) de toiture : à préciser (information non disponible automatiquement)."]
    lines = []
    for g in roof_grids:
        versant = g.get("versant") or "non déterminé"
        slope = g.get("slope")
        n = g.get("nb_panneaux")
        slope_txt = f", pente d'environ {slope:.0f}°" if slope is not None else ""
        lines.append(f"Versant {versant}{slope_txt} : {n} panneau(x).")
    return lines


def generate_notice_dpc11(
    output_path: Path,
    nb_panneaux: int,
    puissance_crete_kwc: float,
    roof_grids: Optional[list] = None,
    adresse_site: str = "",
) -> Path:
    """Genere la notice DPC11 (materiaux utilises + modalites d'execution
    des travaux), piece PDF a part entiere du dossier GNAU.

    Necessite 'reportlab' (cf. requirements.txt)."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, ListFlowable, ListItem
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_JUSTIFY

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    styles = getSampleStyleSheet()
    title_style = styles["Heading1"]
    h2_style = styles["Heading2"]
    body_style = ParagraphStyle("body", parent=styles["BodyText"], alignment=TA_JUSTIFY, spaceAfter=8)

    story = [
        Paragraph("DPC11 — Notice descriptive des matériaux et modalités d'exécution", title_style),
        Spacer(1, 0.3 * cm),
    ]
    if adresse_site:
        story.append(Paragraph(f"Projet : installation photovoltaïque — {adresse_site}", body_style))
    story.append(Paragraph(
        f"Puissance crête installée : {puissance_crete_kwc:.1f} kWc — {nb_panneaux} panneau(x) photovoltaïque(s).",
        body_style,
    ))
    story.append(Spacer(1, 0.4 * cm))

    story.append(Paragraph("1. Matériaux utilisés", h2_style))
    story.append(Paragraph(NOTICE_MATERIAUX_TEXTE, body_style))
    story.append(Spacer(1, 0.3 * cm))

    story.append(Paragraph("2. Modalités d'exécution des travaux", h2_style))
    story.append(Paragraph(NOTICE_MODALITES_TEXTE, body_style))
    story.append(Spacer(1, 0.3 * cm))

    story.append(Paragraph("3. Versant(s) de toiture équipé(s)", h2_style))
    story.append(ListFlowable(
        [ListItem(Paragraph(line, body_style)) for line in _format_versants_lines(roof_grids)],
        bulletType="bullet",
    ))

    doc = SimpleDocTemplate(str(output_path), pagesize=A4,
                             leftMargin=2 * cm, rightMargin=2 * cm, topMargin=2 * cm, bottomMargin=2 * cm)
    doc.build(story)
    return output_path


# ---------------------------------------------------------------------------
# 1ter. Bordereau des pièces jointes (page 13/14 du CERFA) -- cases à cocher
# ---------------------------------------------------------------------------
#
# Ces cases ("DPC1", "DPC2", ... "DPC11") ne sont PAS des champs AcroForm :
# ce sont des cases dessinées statiquement dans le PDF (rectangle + coche),
# donc fill_cerfa() (qui ne touche que l'AcroForm) ne peut pas les cocher.
# On les coche donc en surimposant un petit graphique de coche directement
# sur la page, au bon endroit -- pour CHAQUE piece reellement presente dans
# le dossier GNAU (pas seulement DPC11), afin que le bordereau corresponde
# toujours a la realite du dossier sans intervention manuelle.
#
# Coordonnees mesurees sur templates/cerfa_DPC_1_1.pdf (pdfplumber, systeme
# "top" = distance depuis le haut de page). Chaque case fait environ 8x8 pt,
# demarre 1.5 pt au-dessus du haut du texte du label correspondant, sur les
# pages 13 (DPC1/DPC2) et 14 (DPC4/DPC6/DPC7/DPC8/DPC11) du gabarit CERFA
# 16702*02 (376 champs) utilise par ENEREV.

BORDEREAU_CHECKBOX_COORDS = {
    # piece : (page_index_0based, x0, box_width, box_top, box_height)
    "DP1": (12, 55.5239, 8.004, 568.47, 8.00),
    "DP2": (12, 55.5239, 8.004, 690.47, 8.00),
    "DP4": (13, 55.5239, 8.004, 32.84, 8.00),
    "DP6": (13, 55.5239, 8.004, 201.13, 8.00),
    "DP7": (13, 55.5239, 8.004, 241.63, 8.00),
    "DP8": (13, 55.5239, 8.004, 270.14, 8.00),
    "DP11": (13, 55.5239, 8.004, 541.75, 8.00),
}


def _draw_checkmark_overlay(canvas_obj, x0: float, box_w: float, top: float, box_h: float, page_h: float):
    """Dessine une coche vectorielle (2 segments) dans la case [x0, x0+box_w] x
    [top, top+box_h] (coordonnees 'top' -> converties en repere bas-gauche
    pour reportlab). Pas de dependance a une image externe."""
    pad = box_h * 0.22
    x_left, x_mid, x_right = x0 + pad, x0 + box_w * 0.42, x0 + box_w - pad * 0.6
    y_top_pdf = page_h - top  # haut de la case, repere bas-gauche
    y_left = y_top_pdf - box_h * 0.55
    y_mid = y_top_pdf - box_h * 0.78
    y_right = y_top_pdf - box_h * 0.22

    canvas_obj.setStrokeColorRGB(0.04, 0.04, 0.04)
    canvas_obj.setLineWidth(1.3)
    canvas_obj.setLineCap(1)  # round cap
    canvas_obj.setLineJoin(1)
    p = canvas_obj.beginPath()
    p.moveTo(x_left, y_left)
    p.lineTo(x_mid, y_mid)
    p.lineTo(x_right, y_right)
    canvas_obj.drawPath(p, stroke=1, fill=0)


def stamp_bordereau_checkboxes(cerfa_path: Path, pieces_present) -> None:
    """Coche, directement sur le PDF `cerfa_path` (deja rempli par
    fill_cerfa), les cases du bordereau des pieces jointes correspondant aux
    cles presentes dans `pieces_present` (parmi "DP1","DP2","DP4","DP6",
    "DP7","DP8","DP11"). Modifie le fichier sur place. Les cles inconnues ou
    sans coordonnees enregistrees sont ignorees silencieusement (mieux vaut
    un bordereau partiellement coche qu'une exception qui bloque tout le
    dossier)."""
    from reportlab.pdfgen import canvas as _canvas

    cerfa_path = Path(cerfa_path)
    reader = PdfReader(str(cerfa_path))
    page_h = float(reader.pages[0].mediabox.height)
    page_w = float(reader.pages[0].mediabox.width)

    by_page = {}
    for key in pieces_present:
        coords = BORDEREAU_CHECKBOX_COORDS.get(key)
        if coords:
            by_page.setdefault(coords[0], []).append(coords)

    if not by_page:
        return

    writer = PdfWriter()
    writer.append(reader)

    for page_index, boxes in by_page.items():
        buf = io.BytesIO()
        c = _canvas.Canvas(buf, pagesize=(page_w, page_h))
        for _, x0, box_w, top, box_h in boxes:
            _draw_checkmark_overlay(c, x0, box_w, top, box_h, page_h)
        c.save()
        buf.seek(0)
        overlay_page = PdfReader(buf).pages[0]
        writer.pages[page_index].merge_page(overlay_page)

    with open(cerfa_path, "wb") as f:
        writer.write(f)


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
    "DP11": "DPC11_1_1.pdf",  # notice materiaux / modalites d'execution
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
