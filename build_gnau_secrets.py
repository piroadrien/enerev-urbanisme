"""
Genere un bloc TOML pret a coller dans les secrets Streamlit (Community
Cloud : parametres de l'app > "Secrets"), a partir d'un export CSV KeePass
(Title, User Name, Password, URL).

Usage :
    python build_gnau_secrets.py annuaire_gnau.csv > gnau_secrets.toml

Le code INSEE de chaque commune est resolu via l'API Adresse (BAN), la
meme API que celle utilisee par generate_dp.py -- donc les cles generees
correspondent exactement aux geo["citycode"] que verra l'app au moment du
depot.

IMPORTANT : le fichier .toml produit contient les mots de passe en clair.
Une fois colle dans les secrets Streamlit, supprime-le de ton disque (et
le CSV source aussi).
"""

import csv
import sys
import time
from pathlib import Path

import requests

BAN_SEARCH_URL = "https://api-adresse.data.gouv.fr/search/"


def resolve_insee(commune_name: str) -> dict | None:
    params = {"q": commune_name, "type": "municipality", "limit": 1}
    r = requests.get(BAN_SEARCH_URL, params=params, timeout=10)
    r.raise_for_status()
    features = r.json().get("features", [])
    if not features:
        return None
    props = features[0]["properties"]
    return {"citycode": props["citycode"], "label": props["label"]}


def toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def main():
    # Force l'UTF-8 en sortie, independamment de l'encodage par defaut de la
    # console (cp1252 sous Windows) -- sinon les accents ecrits vers le
    # fichier .toml (via redirection stdout) sont corrompus silencieusement.
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

    if len(sys.argv) != 2:
        print("Usage : python build_gnau_secrets.py <export.csv>", file=sys.stderr)
        sys.exit(1)

    csv_path = Path(sys.argv[1])
    if not csv_path.exists():
        print(f"Fichier introuvable : {csv_path}", file=sys.stderr)
        sys.exit(1)

    unresolved = []
    skipped_no_url = []
    blocks = ["[gnau]"]

    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        sample = f.read(4096)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(f, dialect=dialect)
        fieldmap = {name.strip().lower(): name for name in reader.fieldnames or []}
        # Les differents exporteurs CSV de KeePass 2.x n'utilisent pas tous
        # les memes noms de colonnes (ni le meme delimiteur, detecte ci-dessus) :
        # "KeePass CSV (1.x)" -> Title/User Name/URL, virgules ;
        # export CSV generique -> Account/Login Name/Web Site, point-virgules.
        title_col = fieldmap.get("title") or fieldmap.get("account") or fieldmap.get("name")
        url_col = fieldmap.get("url") or fieldmap.get("web site") or fieldmap.get("website")
        user_col = fieldmap.get("user name") or fieldmap.get("username") or fieldmap.get("login name") or fieldmap.get("login")
        pass_col = fieldmap.get("password")
        if not title_col or not url_col:
            print(f"Colonnes trouvees : {list(fieldmap.values())}", file=sys.stderr)
            print("Le CSV doit contenir au minimum une colonne nom de commune (Title/Account) et une URL (URL/Web Site).", file=sys.stderr)
            sys.exit(1)

        for row in reader:
            commune = (row.get(title_col) or "").strip()
            url = (row.get(url_col) or "").strip()
            if not commune:
                continue
            if not url:
                skipped_no_url.append(commune)
                continue

            print(f"Resolution INSEE pour '{commune}'...", end=" ", file=sys.stderr)
            try:
                match = resolve_insee(commune)
            except requests.RequestException as exc:
                print(f"ERREUR reseau ({exc}), ignore.", file=sys.stderr)
                unresolved.append(commune)
                continue

            if not match:
                print("non trouve -- a ajouter manuellement.", file=sys.stderr)
                unresolved.append(commune)
                continue

            print(f"INSEE {match['citycode']} ({match['label']})", file=sys.stderr)
            username = (row.get(user_col) or "").strip() if user_col else ""
            password = (row.get(pass_col) or "").strip() if pass_col else ""

            blocks.append(f'[gnau."{match["citycode"]}"]')
            blocks.append(f'nom = "{toml_escape(commune)}"')
            blocks.append(f'url = "{toml_escape(url)}"')
            blocks.append(f'username = "{toml_escape(username)}"')
            blocks.append(f'password = "{toml_escape(password)}"')
            blocks.append("")
            time.sleep(0.1)

    print("\n".join(blocks))

    if unresolved:
        print("\n# A verifier/ajouter manuellement (nom introuvable ou ambigu) :", file=sys.stderr)
        for name in unresolved:
            print(f"#   - {name}", file=sys.stderr)

    if skipped_no_url:
        print("\n# Ignorees (pas d'URL renseignee dans KeePass) :", file=sys.stderr)
        for name in skipped_no_url:
            print(f"#   - {name}", file=sys.stderr)


if __name__ == "__main__":
    main()
