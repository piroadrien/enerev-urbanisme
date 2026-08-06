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

import shutil
import tempfile
import traceback
from pathlib import Path

import streamlit as st

from generate_dp import run_pipeline
from cerfa_export import build_gnau_package

st.set_page_config(page_title="Generateur de dossier DP", page_icon="📐")
st.title("📐 Generateur de dossier DP")
st.caption("A partir d'un numero de projet OpenSolar : cadastre, panneaux, Street View, et mise en page QGIS pretes.")

with st.form("dp_form"):
    project_id = st.text_input("Numero de projet OpenSolar", placeholder="ex: 9984170")
    dp_type = st.selectbox("Type d'installation", ["Toiture photovoltaïque", "Carport photovoltaïque"])
    postcode = st.text_input("Code postal (optionnel, aide au geocodage si adresse ambigue)")
    moa_adresse = st.text_input("Adresse du MOA si differente de l'adresse du site (optionnel)")
    submitted = st.form_submit_button("Generer le dossier")

if "dp_result" not in st.session_state:
    st.session_state.dp_result = None

if submitted:
    if not project_id.strip():
        st.error("Merci de renseigner un numero de projet.")
        st.stop()

    st.session_state.dp_result = None  # efface le resultat precedent tant que le nouveau n'est pas pret
    status = st.status("Generation en cours...", expanded=True)

    def log(msg):
        status.write(msg)

    try:
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp) / "projets"
            result = run_pipeline(
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

            status.update(label="Termine !", state="complete", expanded=True)

            project_dir = result.project_dir

            # .qgz + tous ses fichiers de donnees (.gpkg, .jpg) sont
            # references par chemin relatif depuis le .qgz -- il faut les
            # livrer ensemble, pas le .qgz seul, sinon le projet est casse
            # a l'ouverture (couches introuvables).
            zip_base = Path(tmp) / project_dir.name
            zip_path = shutil.make_archive(str(zip_base), "zip", root_dir=project_dir)
            with open(zip_path, "rb") as f:
                zip_bytes = f.read()

            # Palier 2 -- export des 6 PDF (DP1/DP2/DP4/DP6/DP7/DP8), puis
            # assemblage du zip pret a importer dans GNAU (cerfa + pieces).
            pdf_bytes = {}
            pdf_error = None
            gnau_zip_bytes = None
            gnau_zip_name = None
            try:
                from pdf_export import export_dp_pdfs
                pdf_dir = Path(tmp) / "pdfs"
                pdfs = export_dp_pdfs(result.qgz, pdf_dir)
                for label, pdf_path in sorted(pdfs.items()):
                    with open(pdf_path, "rb") as f:
                        pdf_bytes[label] = f.read()

                gnau_zip_path = build_gnau_package(
                    Path(tmp) / f"{project_dir.name}_gnau.zip",
                    result.cerfa,
                    pdfs,
                )
                gnau_zip_name = f"{project_dir.name}_gnau.zip"
                with open(gnau_zip_path, "rb") as f:
                    gnau_zip_bytes = f.read()
            except Exception as exc:
                pdf_error = str(exc)

            # CERFA seul (toujours disponible, meme si l'export QGIS headless
            # a echoue -- il ne depend pas de pdf_export.py).
            with open(result.cerfa, "rb") as f:
                cerfa_bytes = f.read()

            # On garde tout en memoire (bytes) dans session_state -- le
            # dossier temporaire (tmp) est detruit a la sortie du 'with',
            # donc il ne faut PAS y stocker de chemins, seulement le
            # contenu deja lu.
            st.session_state.dp_result = {
                "zip_name": f"{project_dir.name}.zip",
                "zip_bytes": zip_bytes,
                "cerfa_bytes": cerfa_bytes,
                "pdf_bytes": pdf_bytes,
                "pdf_error": pdf_error,
                "gnau_zip_name": gnau_zip_name,
                "gnau_zip_bytes": gnau_zip_bytes,
            }

    except Exception as exc:
        status.update(label="Erreur", state="error")
        st.error(f"Erreur : {exc}")
        with st.expander("Detail technique"):
            st.code(traceback.format_exc())

# Affiche les telechargements a partir de session_state -- en dehors du
# bloc 'if submitted', pour que les boutons restent visibles/fonctionnels
# meme apres le rerun declenche par un clic sur un download_button.
result = st.session_state.dp_result
if result:
    st.success("Dossier genere avec succes.")

    choix = st.radio(
        "Que veux-tu telecharger ?",
        ["Projet QGIS (.qgz + donnees)", "Dossier GNAU pret a importer (.zip)"],
        horizontal=True,
    )

    if choix == "Projet QGIS (.qgz + donnees)":
        st.download_button(
            f"⬇️ Telecharger le dossier complet ({result['zip_name']})",
            data=result["zip_bytes"],
            file_name=result["zip_name"],
            mime="application/zip",
            key="dl_zip",
        )
        st.caption("Contient le .qgz et tous les fichiers de donnees associes (cadastre, panneaux, photos) -- decompresse tout dans un meme dossier avant d'ouvrir le .qgz.")

    else:
        if result["gnau_zip_bytes"]:
            st.download_button(
                f"⬇️ Telecharger le dossier GNAU ({result['gnau_zip_name']})",
                data=result["gnau_zip_bytes"],
                file_name=result["gnau_zip_name"],
                mime="application/zip",
                key="dl_gnau_zip",
            )
            st.caption(
                "Contient cerfa_DPC_1_1.pdf, DPC1_1_1.pdf, DPC2_1_1.pdf, DPC4_1_1.pdf, "
                "DPC6_1_1.pdf, DPC7_1_1.pdf et DPC8_1_1.pdf -- a deposer tel quel via "
                "\"Importer le dossier\" > \"Import du formulaire et des pieces\" sur GNAU."
            )
        else:
            st.warning(
                "Export des 6 PDF (DP1/2/4/6/7/8) indisponible pour l'instant"
                + (f" : {result['pdf_error']}" if result["pdf_error"] else "")
                + " -- le CERFA seul reste telechargeable ci-dessous."
            )
            st.download_button(
                "⬇️ Telecharger le CERFA seul (cerfa_DPC_1_1.pdf)",
                data=result["cerfa_bytes"],
                file_name="cerfa_DPC_1_1.pdf",
                mime="application/pdf",
                key="dl_cerfa_only",
            )
            st.caption(
                "En attendant que l'export QGIS headless (pdf_export.py) soit valide : "
                "exporte DP1/DP2/DP4/DP6/DP7/DP8 manuellement depuis le .qgz, puis "
                "zippe-les avec ce CERFA sous les noms DPC1_1_1.pdf ... DPC8_1_1.pdf."
            )
