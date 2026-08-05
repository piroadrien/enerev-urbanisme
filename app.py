"""
Application Streamlit : genere un dossier DP (Declaration Prealable) a
partir d'un numero de projet OpenSolar.

Palier 1 (implemente ici) : produit toujours le .qgz -- aucune dependance a
QGIS, ca tient largement dans les limites de Streamlit Community Cloud.

Palier 2 (export des 6 PDF) : necessite QGIS installe sur la machine qui
execute l'app (voir packages.txt). Sur Community Cloud (~1 Go de RAM), ce
n'est pas garanti de tenir selon la taille du projet -- voir pdf_export.py
et le README pour le plan de repli (delegation a GitHub Actions) si ca
depasse les limites.
"""

import tempfile
import traceback
from pathlib import Path

import streamlit as st

from generate_dp import run_pipeline

st.set_page_config(page_title="Generateur de dossier DP", page_icon="📐")
st.title("📐 Generateur de dossier DP")
st.caption("A partir d'un numero de projet OpenSolar : cadastre, panneaux, Street View, et mise en page QGIS pretes.")

with st.form("dp_form"):
    project_id = st.text_input("Numero de projet OpenSolar", placeholder="ex: 9984170")
    dp_type = st.selectbox("Type d'installation", ["Toiture photovoltaïque", "Carport photovoltaïque"])
    postcode = st.text_input("Code postal (optionnel, aide au geocodage si adresse ambigue)")
    moa_adresse = st.text_input("Adresse du MOA si differente de l'adresse du site (optionnel)")
    submitted = st.form_submit_button("Generer le dossier")

if submitted:
    if not project_id.strip():
        st.error("Merci de renseigner un numero de projet.")
        st.stop()

    status = st.status("Generation en cours...", expanded=True)
    log_lines = []

    def log(msg):
        log_lines.append(str(msg))
        status.write(msg)

    try:
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp) / "projets"
            out_qgz = run_pipeline(
                project_id=project_id.strip(),
                dp_type=dp_type,
                template=str(Path(__file__).parent / "templates" / "modele_DP_v4.2.qgz"),
                work_dir=str(work_dir),
                # secrets : voir .streamlit/secrets.toml (Community Cloud : onglet "Secrets" de l'app)
                username=st.secrets.get("opensolar_username"),
                password=st.secrets.get("opensolar_password"),
                org_id=st.secrets.get("opensolar_org_id"),
                google_api_key=st.secrets.get("google_api_key"),
                moa_adresse=moa_adresse or None,
                postcode=postcode or None,
                log=log,
            )

            status.update(label="Termine !", state="complete")

            st.success("Dossier genere avec succes.")
            with open(out_qgz, "rb") as f:
                st.download_button(
                    "⬇️ Telecharger le .qgz",
                    data=f.read(),
                    file_name=out_qgz.name,
                    mime="application/octet-stream",
                )

            # Palier 2 -- export PDF (voir pdf_export.py) :
            # from pdf_export import export_dp_pdfs
            # try:
            #     pdfs = export_dp_pdfs(out_qgz, tmp)
            #     for label, pdf_path in pdfs.items():
            #         with open(pdf_path, "rb") as f:
            #             st.download_button(f"⬇️ {label}.pdf", f.read(), file_name=f"{label}.pdf", mime="application/pdf")
            # except Exception as exc:
            #     st.warning(f"Export PDF indisponible sur cet environnement : {exc}")

    except Exception as exc:
        status.update(label="Erreur", state="error")
        st.error(f"Erreur : {exc}")
        with st.expander("Detail technique"):
            st.code(traceback.format_exc())
