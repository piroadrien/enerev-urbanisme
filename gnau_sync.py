"""
Synchronisation du suivi DP : lit les nouveaux emails de notification GNAU
(boite urbanisme@enerev.fr), met a jour la liste SharePoint "Suivi DP", et
notifie le client par email quand son statut change.

Concu pour tourner comme un job planifie independant (GitHub Actions), pas
depuis l'app Streamlit elle-meme (qui n'est active que quand quelqu'un a la
page ouverte -- pas adapte a une verification reguliere de boite mail).

Logique de rapprochement (validee le 2026-09-10) :
  - Les emails GNAU ne mentionnent jamais le client, seulement la commune.
  - Le premier evenement exploitable est l'"accuse de reception", qui donne
    a la fois la commune ET le numero de DP definitif : on l'utilise pour
    relier un email a une ligne "Genere" (creee par l'app au moment de la
    generation du dossier) filtree par commune (INSEE) sans NumeroDP encore
    connu. Une seule ligne candidate -> rapprochement automatique. Plusieurs
    candidates -> la ligne reste "A valider" (pas de devinette).
  - Une fois le NumeroDP connu, tous les emails suivants (completude,
    incompletude, decision, delai) le referencent explicitement : mise a
    jour directe et non ambigue par numero de DP.
"""

import os
import re
import sys
from typing import Optional

import requests

import ms_graph
from email_templates import CLIENT_TEMPLATES, INTERNAL_REFUS_TEMPLATE, render
from gnau_email_parser import STATUT_ACCORDEE, STATUT_ENVOYEE, STATUT_REFUSEE, classify_email, normalize_dp_number

BAN_SEARCH_URL = "https://api-adresse.data.gouv.fr/search/"


def config_from_env() -> dict:
    """Construit la config depuis des variables d'environnement -- pour le
    job planifie (GitHub Actions), qui n'a pas acces a st.secrets."""
    return {
        "tenant_id": os.environ["MS_TENANT_ID"],
        "client_id": os.environ["MS_CLIENT_ID"],
        "client_secret": os.environ["MS_CLIENT_SECRET"],
        "site_id": os.environ["MS_SITE_ID"],
        "list_id": os.environ["MS_LIST_ID"],
        "mailbox": os.environ.get("MS_MAILBOX", "urbanisme@enerev.fr"),
    }


def resolve_insee(commune_name: str) -> Optional[str]:
    """Meme resolution que build_gnau_secrets.py -- API Adresse (BAN)."""
    if not commune_name:
        return None
    try:
        r = requests.get(BAN_SEARCH_URL, params={"q": commune_name, "type": "municipality", "limit": 1}, timeout=10)
        r.raise_for_status()
        features = r.json().get("features", [])
        return features[0]["properties"]["citycode"] if features else None
    except requests.RequestException:
        return None


def _row_fields(item: dict) -> dict:
    return item.get("fields", {})


def find_generated_row_by_commune(rows: list[dict], insee: str) -> list[dict]:
    """Lignes 'Genere' pour cette commune, sans NumeroDP encore connu --
    candidates pour le rapprochement automatique par accuse de reception."""
    candidates = []
    for row in rows:
        f = _row_fields(row)
        if f.get("Statut") == "Généré" and f.get("INSEE") == insee and not f.get("NumeroDP"):
            candidates.append(row)
    return candidates


def find_row_by_dp_number(rows: list[dict], dp_number: str) -> Optional[dict]:
    target = normalize_dp_number(dp_number)
    for row in rows:
        existing = _row_fields(row).get("NumeroDP")
        if existing and normalize_dp_number(existing) == target:
            return row
    return None


def process_event(config: dict, rows: list[dict], event, received_date_iso: Optional[str] = None) -> Optional[dict]:
    """Applique un evenement classifie a la liste SharePoint (in-place sur
    `rows`, et pousse la mise a jour vers SharePoint). Renvoie un dict
    resumant l'action prise (pour les logs du job), ou None si rien a faire."""

    row = find_row_by_dp_number(rows, event.dp_number) if event.dp_number else None

    if row is None and event.raw_kind == "accuse_reception" and event.dp_number and event.commune:
        insee = resolve_insee(event.commune)
        if insee:
            candidates = find_generated_row_by_commune(rows, insee)
            if len(candidates) == 1:
                row = candidates[0]
            elif len(candidates) > 1:
                return {"action": "ambigu", "commune": event.commune, "dp_number": event.dp_number,
                        "detail": f"{len(candidates)} lignes 'Genere' candidates pour INSEE {insee}, aucune retenue automatiquement"}
            # 0 candidat : email pour un dossier non genere par l'app (ou deja rapproche) -- on ignore.

    if row is None or event.status is None:
        return None

    fields_update = {"Statut": event.status}
    if event.dp_number and not _row_fields(row).get("NumeroDP"):
        fields_update["NumeroDP"] = event.dp_number
    if event.date_depot and not _row_fields(row).get("DateDepot"):
        fields_update["DateDepot"] = _to_iso_date(event.date_depot)
    if event.date_limite:
        fields_update["DateEstimee"] = _to_iso_date(event.date_limite)
    if event.status in (STATUT_ACCORDEE, STATUT_REFUSEE) and received_date_iso:
        # les emails de decision ne contiennent pas de date formatee dans le
        # corps -- on utilise la date de reception de l'email comme date
        # reelle de decision (approximation raisonnable, a quelques heures pres).
        fields_update["DateReelle"] = received_date_iso

    previous_status = _row_fields(row).get("Statut")
    ms_graph.update_list_item(config, row["id"], fields_update)
    _row_fields(row).update(fields_update)  # garde `rows` a jour pour les evenements suivants du meme lot

    notified = False
    if event.status != previous_status:
        notified = _notify_status_change(config, row, event.status)

    return {"action": "maj", "row_id": row["id"], "statut": event.status, "dp_number": event.dp_number, "notifie": notified}


def _to_iso_date(date_ddmmyyyy: str) -> Optional[str]:
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", date_ddmmyyyy or "")
    if not m:
        return None
    d, mo, y = m.groups()
    return f"{y}-{mo}-{d}"


def _notify_status_change(config: dict, row: dict, new_status: str) -> bool:
    fields = _row_fields(row)
    format_fields = {
        "NumeroDP": fields.get("NumeroDP", ""),
        "Ville": fields.get("Ville", ""),
        "DateDepot": fields.get("DateDepot", ""),
        "DateEstimee": fields.get("DateEstimee", ""),
        "Client": fields.get("Title", ""),
    }

    if new_status == STATUT_REFUSEE:
        subject, body = render(INTERNAL_REFUS_TEMPLATE, format_fields)
        ms_graph.send_mail(config, config["mailbox"], subject, body)
        return True

    template = CLIENT_TEMPLATES.get(new_status)
    email_client = fields.get("EmailClient")
    if template and email_client:
        subject, body = render(template, format_fields)
        ms_graph.send_mail(config, email_client, subject, body)
        return True
    return False


def run_sync(config: dict) -> list[dict]:
    rows = ms_graph.get_all_list_items(config)
    messages = ms_graph.get_unprocessed_messages(config)
    results = []

    for message in reversed(messages):  # plus ancien -> plus recent, pour respecter l'ordre chronologique
        body_text = ms_graph.get_message_body_text(message)
        event = classify_email(message.get("subject", ""), body_text)
        received_date_iso = (message.get("receivedDateTime") or "")[:10] or None
        outcome = process_event(config, rows, event, received_date_iso)
        if outcome:
            results.append(outcome)
        ms_graph.mark_message_processed(config, message["id"])

    return results


if __name__ == "__main__":
    cfg = config_from_env()
    outcomes = run_sync(cfg)
    for o in outcomes:
        print(o)
    if not outcomes:
        print("Aucun changement de statut detecte.")
    sys.exit(0)
