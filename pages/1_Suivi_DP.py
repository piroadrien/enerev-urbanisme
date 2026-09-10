"""
Page de suivi des dossiers DP en cours -- lit la liste SharePoint "Suivi DP"
(mise a jour par le job planifie gnau_sync.py a partir des notifications
GNAU) et l'affiche sous forme de tableau, avec les identifiants GNAU de
chaque commune accessibles en un clic.
"""

import pandas as pd
import streamlit as st

import ms_graph

st.set_page_config(page_title="Suivi des DP", page_icon="📋")
st.title("📋 Suivi des dossiers DP")

sp_config = ms_graph.config_from_secrets(st.secrets)
if not sp_config:
    st.warning(
        "Le suivi SharePoint n'est pas configure (section [ms365] manquante dans les secrets Streamlit). "
        "Les lignes sont normalement creees automatiquement a chaque generation de dossier sur la page principale."
    )
    st.stop()

try:
    with st.spinner("Chargement depuis SharePoint..."):
        items = ms_graph.get_all_list_items(sp_config)
except Exception as exc:
    st.error(f"Erreur lors de la lecture de la liste SharePoint : {exc}")
    st.stop()

if not items:
    st.info("Aucun dossier DP suivi pour l'instant.")
    st.stop()

COLONNES_AFFICHEES = [
    "Title", "Adresse", "Ville", "NumeroDP", "Statut",
    "DateGeneration", "DateDepot", "DateEstimee", "DateReelle",
]
LABELS = {
    "Title": "Client", "Adresse": "Adresse", "Ville": "Ville", "NumeroDP": "N° DP",
    "Statut": "Statut", "DateGeneration": "Généré le", "DateDepot": "Déposé le",
    "DateEstimee": "Date estimée", "DateReelle": "Date réelle",
}

rows = [{col: (it.get("fields", {}).get(col) or "") for col in COLONNES_AFFICHEES} for it in items]
df = pd.DataFrame(rows).rename(columns=LABELS)

st.dataframe(df, use_container_width=True, hide_index=True)

st.subheader("Accès GNAU par dossier")
gnau_directory = st.secrets.get("gnau", {})
for it in items:
    f = it.get("fields", {})
    insee = f.get("INSEE", "")
    entry = gnau_directory.get(insee)
    with st.expander(f"{f.get('Title', '(sans nom)')} — {f.get('Ville', '')} — {f.get('Statut', '')}"):
        if entry:
            st.link_button(f"🔗 Ouvrir le portail GNAU de {entry.get('nom', insee)}", entry["url"])
            col1, col2 = st.columns(2)
            with col1:
                st.caption("Identifiant")
                st.code(entry.get("username", ""), language=None)
            with col2:
                st.caption("Mot de passe")
                st.code(entry.get("password", ""), language=None)
        else:
            st.info(f"Portail GNAU non répertorié pour cette commune (INSEE {insee}).")
        if f.get("Commentaire"):
            st.caption(f"Commentaire : {f['Commentaire']}")
