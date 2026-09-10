"""
Client Microsoft Graph minimal pour le suivi des DP :
- authentification (OAuth2 client credentials, via msal -- pas de MFA/utilisateur
  implique, adapte a un script planifie sans surveillance) ;
- lecture/ecriture de la liste SharePoint "Suivi DP" ;
- lecture des emails de la boite urbanisme@enerev.fr et envoi de notifications.

Toutes les fonctions prennent une config explicite (dict) plutot que de lire
st.secrets directement, pour rester utilisables aussi bien depuis l'app
Streamlit que depuis le script d'ingestion planifie (GitHub Actions), qui n'a
pas Streamlit installe.
"""

import time
from typing import Optional

import msal
import requests

GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# Categorie posee sur un email une fois traite par le script d'ingestion --
# evite de le retraiter au prochain passage (pas d'etat externe a maintenir :
# la boite mail elle-meme fait office de marqueur).
PROCESSED_CATEGORY = "GNAU-Traite"


def config_from_secrets(secrets) -> Optional[dict]:
    """Construit la config depuis st.secrets["ms365"] -- utilise cote app
    Streamlit (creation de la ligne "Genere" a la volee). Renvoie None si la
    section n'est pas configuree, pour que l'app degrade proprement (pas de
    ligne de suivi creee, mais le reste du pipeline continue de fonctionner)
    plutot que de planter si le suivi SharePoint n'est pas encore branche."""
    ms365 = secrets.get("ms365")
    if not ms365:
        return None
    required = ("tenant_id", "client_id", "client_secret", "site_id", "list_id")
    if not all(ms365.get(k) for k in required):
        return None
    return {
        "tenant_id": ms365["tenant_id"],
        "client_id": ms365["client_id"],
        "client_secret": ms365["client_secret"],
        "site_id": ms365["site_id"],
        "list_id": ms365["list_id"],
        "mailbox": ms365.get("mailbox", "urbanisme@enerev.fr"),
    }


def get_token(config: dict) -> str:
    """config attendu : {"tenant_id", "client_id", "client_secret"}."""
    app = msal.ConfidentialClientApplication(
        config["client_id"],
        authority=f"https://login.microsoftonline.com/{config['tenant_id']}",
        client_credential=config["client_secret"],
    )
    result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
    if "access_token" not in result:
        raise RuntimeError(f"Echec authentification Graph : {result.get('error')} - {result.get('error_description')}")
    return result["access_token"]


def _headers(config: dict) -> dict:
    return {"Authorization": f"Bearer {get_token(config)}", "Content-Type": "application/json"}


def _raise_for_status(r: requests.Response):
    if not r.ok:
        raise RuntimeError(f"Graph API {r.request.method} {r.url} -> HTTP {r.status_code} : {r.text[:500]}")


# ═════════════════════════════════════════════════════════════
# Liste SharePoint "Suivi DP"
# ═════════════════════════════════════════════════════════════

def list_items_url(config: dict) -> str:
    return f"{GRAPH_BASE}/sites/{config['site_id']}/lists/{config['list_id']}/items"


def create_list_item(config: dict, fields: dict) -> dict:
    r = requests.post(list_items_url(config), headers=_headers(config), json={"fields": fields})
    _raise_for_status(r)
    return r.json()


def update_list_item(config: dict, item_id: str, fields: dict) -> dict:
    url = f"{list_items_url(config)}/{item_id}/fields"
    r = requests.patch(url, headers=_headers(config), json=fields)
    _raise_for_status(r)
    return r.json()


def get_all_list_items(config: dict) -> list[dict]:
    """Renvoie tous les items avec leurs champs (fields expand) -- la liste
    de suivi reste petite (quelques dizaines/centaines de lignes), pas besoin
    de pagination fine cote appelant : on suit nous-memes @odata.nextLink."""
    items = []
    url = f"{list_items_url(config)}?$expand=fields&$top=200"
    headers = _headers(config)
    while url:
        r = requests.get(url, headers=headers)
        _raise_for_status(r)
        data = r.json()
        items.extend(data.get("value", []))
        url = data.get("@odata.nextLink")
    return items


# ═════════════════════════════════════════════════════════════
# Mail (boite urbanisme@enerev.fr)
# ═════════════════════════════════════════════════════════════

def get_unprocessed_messages(config: dict, top: int = 50) -> list[dict]:
    """Messages recents de la boite de notification GNAU qui n'ont pas encore
    ete traites (pas de categorie PROCESSED_CATEGORY) -- filtrage cote client
    (volume attendu faible, pas besoin d'un filtre OData sur 'categories')."""
    mailbox = config["mailbox"]
    url = (
        f"{GRAPH_BASE}/users/{mailbox}/mailFolders/inbox/messages"
        f"?$top={top}&$orderby=receivedDateTime desc"
        f"&$select=id,subject,receivedDateTime,categories,body,from"
    )
    r = requests.get(url, headers=_headers(config))
    _raise_for_status(r)
    messages = r.json().get("value", [])
    return [m for m in messages if PROCESSED_CATEGORY not in (m.get("categories") or [])]


def get_message_body_text(message: dict) -> str:
    body = message.get("body") or {}
    content = body.get("content") or ""
    if body.get("contentType") == "html":
        from bs4 import BeautifulSoup
        return BeautifulSoup(content, "html.parser").get_text("\n")
    return content


def mark_message_processed(config: dict, message_id: str):
    mailbox = config["mailbox"]
    url = f"{GRAPH_BASE}/users/{mailbox}/messages/{message_id}"
    r = requests.patch(url, headers=_headers(config), json={"categories": [PROCESSED_CATEGORY]})
    _raise_for_status(r)


def send_mail(config: dict, to_address: str, subject: str, body_text: str, from_mailbox: Optional[str] = None):
    """Envoie un email via Graph (permission d'application Mail.Send) --
    depuis from_mailbox (par defaut config['mailbox'], la boite urbanisme).
    Pas besoin de delegation Send As / Send on Behalf : une permission
    d'application Mail.Send peut deja envoyer au nom de n'importe quelle
    boite du tenant."""
    mailbox = from_mailbox or config["mailbox"]
    url = f"{GRAPH_BASE}/users/{mailbox}/sendMail"
    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "Text", "content": body_text},
            "toRecipients": [{"emailAddress": {"address": to_address}}],
        },
        "saveToSentItems": True,
    }
    r = requests.post(url, headers=_headers(config), json=payload)
    _raise_for_status(r)
