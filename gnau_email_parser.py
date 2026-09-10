"""
Analyse des emails de notification GNAU (Guichet Numerique des Autorisations
d'Urbanisme) pour en extraire le statut d'un dossier de Declaration Prealable.

La plupart des communes utilisent le meme socle logiciel national (GNAU), donc
les sujets et corps de mail sont tres proches d'un portail a l'autre malgre des
domaines d'expediteur differents (Operis, Cergy-Pontoise, Geosphere,
Marne-et-Gondoire, etc.) -- cf. analyse menee sur 33 emails reels le 2026-09-10.

classify_email() ne fait AUCUN appel reseau : c'est une fonction pure
(sujet + corps -> evenement structure), testable independamment de la
recuperation des emails (Graph API) et de leur rapprochement avec un projet.
"""

import re
from dataclasses import dataclass
from typing import Optional

# Statuts possibles pour une ligne du suivi DP.
STATUT_GENERE = "Généré"          # dossier genere par l'app, pas encore depose sur GNAU
STATUT_ENVOYEE = "Envoyée"        # accuse d'enregistrement et/ou de reception recu
STATUT_COMPLETE = "Complète"
STATUT_INCOMPLETE = "Incomplète"
STATUT_ACCORDEE = "Accordée"
STATUT_REFUSEE = "Refusée"

# Regex du numero de DP definitif. Formats reellement observes :
#   "DP 78133 26 G0057", "DP 92063 26 00315", "DP 095476 26U0116" (pas
#   d'espace avant la lettre de section), "DP 092 009 26 00126" (bloc INSEE
#   lui-meme separe par un espace interne).
# Plutot que d'essayer de decouper insee/annee/section (ambigu : la position
# des espaces varie), on capture tout le bloc alphanumerique qui suit "DP" et
# on ne retient le resultat que s'il contient assez de chiffres pour etre un
# vrai numero de dossier (>= 7, le minimum observe : insee 5 + annee 2).
# "DP" lui-meme est insensible a la casse (flag local (?i:...)) mais PAS le
# reste du motif : sans ca, sous re.IGNORECASE, des mots ordinaires en
# minuscule comme "et"/"le" satisfont [0-9A-Z] et se retrouvent avales dans
# le numero (ex: ".. 78146 26 02203 et recue.." -> ".. 02203 ET"). Les
# sections de numero de dossier sont toujours en MAJUSCULES dans les emails.
DP_NUMBER_RE = re.compile(r"(?i:DP)\s?[0-9A-Z](?:[0-9A-Z ]{3,15}[0-9A-Z])?")


def normalize_dp_number(raw: str) -> str:
    """Met un numero de DP dans une forme canonique comparable (espaces
    normalises, un seul format) -- deux emails qui referencent le meme
    dossier peuvent l'ecrire avec un espacement legerement different."""
    return re.sub(r"\s+", " ", raw.strip().upper())


def extract_dp_number(text: str) -> Optional[str]:
    for m in DP_NUMBER_RE.finditer(text):
        raw = m.group(0)
        if len(re.sub(r"\D", "", raw)) >= 7:
            return normalize_dp_number(raw)
    return None


DATE_RE = r"(\d{2}/\d{2}/\d{4})"


def _find_date_after(body: str, keyword_pattern: str) -> Optional[str]:
    m = re.search(keyword_pattern + r".{0,20}?" + DATE_RE, body, re.IGNORECASE | re.DOTALL)
    return m.group(1) if m else None


@dataclass
class GnauEvent:
    subject: str
    status: Optional[str]          # une des constantes STATUT_* ci-dessus, ou None si non reconnu
    dp_number: Optional[str]       # numero definitif si present dans l'email
    numero_provisoire: Optional[str]  # numero d'enregistrement provisoire (avant le n° DP definitif)
    commune: Optional[str]         # nom de commune si identifiable dans le corps
    date_depot: Optional[str]      # date de depot/reception en mairie (JJ/MM/AAAA)
    date_limite: Optional[str]     # date limite de decision (complet) ou nouveau delai
    is_piece_complementaire: bool  # evenement secondaire (depot de pieces), pas un changement de statut principal
    raw_kind: str                  # etiquette technique du type d'email reconnu, pour debug/logs


def classify_email(subject: str, body: str) -> GnauEvent:
    subject = (subject or "").strip()
    # les octets nuls (artefact d'extraction de certains .msg) cassent les
    # ancres de fin de ligne (\s ne les absorbe pas) -- on les retire.
    body = (body or "").replace("\x00", "")
    subj_l = subject.lower()

    dp_number = extract_dp_number(subject) or extract_dp_number(body)

    # Numero provisoire : "numero 1591", "numéro 76330" apparaissant dans un
    # accuse d'ENREGISTREMENT (pas de reception) qui ne contient pas encore de n° DP.
    numero_provisoire = None
    m = re.search(r"num[ée]ro\s*(\d{2,7})", body, re.IGNORECASE)
    if m and not dp_number:
        numero_provisoire = m.group(1)

    commune = None
    m = re.search(r"service (?:instructeur|urbanisme) de[^A-Za-z]*([A-ZÀ-Ü][A-ZÀ-Ü\s\-']+)\.?\s*$", body.strip(), re.MULTILINE)
    if m:
        commune = m.group(1).strip().rstrip(".")
    else:
        m = re.search(r"commune de ([A-ZÀ-Ü][A-ZÀ-Ü\s\-']+?)(?:\s+le\s+\d{2}/\d{2}/\d{4}|\.)", body)
        if not m:
            m = re.search(r"[Vv]ille de ([A-ZÀ-Ü][A-ZÀ-Ü\s\-']+?)(?:\s+une\s+demande|[.,])", body)
        if m:
            commune = m.group(1).strip()

    # --- classification par mots-cles du sujet, du plus specifique au plus generique ---
    if re.search(r"d[ée]cision sur votre dossier", subj_l):
        favorable = bool(re.search(r"favorable(?!\w)", body, re.IGNORECASE)) and not re.search(r"d[ée]favorable", body, re.IGNORECASE)
        defavorable = bool(re.search(r"d[ée]favorable|rejet[ée]|refus[ée]", body, re.IGNORECASE))
        status = STATUT_ACCORDEE if favorable and not defavorable else (STATUT_REFUSEE if defavorable else None)
        return GnauEvent(subject, status, dp_number, None, commune, None,
                          None, False, "decision")

    if re.search(r"^compl[ée]tude sur la daact", subj_l):
        return GnauEvent(subject, None, dp_number, None, commune, None, None, False, "daact_completude")

    if re.search(r"incompl[ée]tude sur votre dossier", subj_l):
        return GnauEvent(subject, STATUT_INCOMPLETE, dp_number, None, commune, None, None, False, "incompletude")

    if re.search(r"compl[ée]tude sur votre dossier", subj_l):
        date_limite = _find_date_after(body, r"jusqu.au")
        return GnauEvent(subject, STATUT_COMPLETE, dp_number, None, commune, None, date_limite, False, "completude")

    if re.search(r"d[ée]lai d.instruction", subj_l):
        return GnauEvent(subject, None, dp_number, None, commune, None, None, False, "delai_instruction")

    if re.search(r"accus[ée] de r[ée]ception .*pi[èe]ce", subj_l) or re.search(r"accus[ée] d.enregistrement .*pi[èe]ce", subj_l):
        date_depot = _find_date_after(body, r"le")
        return GnauEvent(subject, None, dp_number, None, commune, date_depot, None, True, "piece_complementaire")

    if re.search(r"d[ée]p[oô]t de pi[èe]ce\(s\) en ligne", subj_l):
        date_depot = _find_date_after(body, r"le")
        return GnauEvent(subject, None, dp_number, None, commune, date_depot, None, True, "piece_complementaire_geosphere")

    # "Depot de dossier en ligne" (Chanteloup-en-Brie) = equivalent d'un accuse
    # de reception (contient deja le n° DP definitif) -- a distinguer de
    # "Dossier en ligne n°..." (Bois-Colombes, nouveau document, plus bas)
    # qui ne contient PAS "depot".
    if re.search(r"accus[ée] de r[ée]ception [ée]lectronique de votre demande", subj_l) or \
       re.search(r"d[ée]p[oô]t de dossier en ligne", subj_l):
        date_depot = _find_date_after(body, r"re[çc]ue? en mairie le") or _find_date_after(body, r"enregistr[ée]e? le")
        return GnauEvent(subject, STATUT_ENVOYEE, dp_number, None, commune, date_depot, None, False, "accuse_reception")

    if re.search(r"dossier en ligne", subj_l):
        # ex Bois-Colombes : nouveau document depose par la mairie, pas un changement de statut
        return GnauEvent(subject, None, dp_number, None, commune, None, None, True, "nouveau_document")

    if re.search(r"accus[ée] d.enregistrement [ée]lectronique", subj_l):
        # accuse d'enregistrement generique (demande initiale OU creation de dossier)
        # -- ne contient que le numero PROVISOIRE, pas encore le n° DP definitif.
        return GnauEvent(subject, STATUT_ENVOYEE, dp_number, numero_provisoire, commune, None, None, False, "accuse_enregistrement")

    if re.search(r"synth[èe]se", subj_l):
        date_depot = _find_date_after(body, r"d[ée]pos[ée] le")
        return GnauEvent(subject, None, dp_number, None, commune, date_depot, None, True, "synthese")

    return GnauEvent(subject, None, dp_number, numero_provisoire, commune, None, None, False, "non_reconnu")
