"""
pdf_export.py -- Palier 2 : export des 6 rapports (DP1, DP2, DP4, DP6, DP7,
DP8) du .qgz en PDF, via QGIS installe en mode headless.

Ces 6 elements sont des RAPPORTS (QgsReport), pas des mises en page
classiques (QgsLayout) -- confirme empiriquement : l'algorithme Processing
generique "native:printlayouttopdf" ne sait chercher que dans le
layoutManager() et echoue avec "Cannot find layout" sur un rapport. On
utilise donc directement l'API PyQGIS (project.reportManager()), dans un
script Python separe execute par l'interpreteur systeme (celui du paquet
apt python3-qgis, qui expose le module 'qgis' -- pas necessairement le
meme interpreteur/venv que Streamlit).

Usage prevu :
    from pdf_export import export_dp_pdfs
    pdfs = export_dp_pdfs(qgz_path, output_dir)
    # -> {"DP1": Path(".../DP1.pdf"), "DP2": Path(...), ...}
"""

import json
import os
import subprocess
from pathlib import Path

REPORT_NAMES = ["DP 1", "DP 2", "DP 4", "DP 6", "DP 7", "DP 8"]

# QGIS/Qt a besoin d'un affichage meme en mode "headless". Plutot que
# d'installer un serveur X virtuel (xvfb), on force le plugin Qt
# "offscreen", qui ne necessite aucune dependance systeme supplementaire.
_HEADLESS_ENV = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}

_WORKER_SCRIPT = """
import sys, json
from qgis.core import QgsApplication, QgsProject, QgsLayoutExporter

qgz_path, output_dir, report_names_json = sys.argv[1], sys.argv[2], sys.argv[3]
report_names = json.loads(report_names_json)

qgs = QgsApplication([], False)
qgs.initQgis()

project = QgsProject.instance()
if not project.read(qgz_path):
    print(json.dumps({"error": f"Impossible d'ouvrir le projet : {qgz_path}"}))
    qgs.exitQgis()
    sys.exit(1)

reports_by_name = {r.name(): r for r in project.reportManager().reports()}
results = {}
for name in report_names:
    label = name.replace(" ", "")
    report = reports_by_name.get(name)
    if report is None:
        results[label] = {"ok": False, "error": f"Rapport '{name}' introuvable (rapports presents : {list(reports_by_name)})"}
        continue
    out_pdf = f"{output_dir}/{label}.pdf"
    settings = QgsLayoutExporter.PdfExportSettings()
    result, error_msg = QgsLayoutExporter.exportToPdf(report, out_pdf, settings)
    if int(result) == 0:  # Success vaut toujours 0 dans cet enum, quelle que soit la version QGIS (API du chemin scope differente selon les versions : QgsLayoutExporter.Success vs .ExportResult.Success)
        results[label] = {"ok": True, "path": out_pdf}
    else:
        results[label] = {"ok": False, "error": str(error_msg)}

qgs.exitQgis()
print(json.dumps(results))
"""


def export_dp_pdfs(qgz_path: Path, output_dir: Path, python_bin: str = "python3") -> dict:
    qgz_path = Path(qgz_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    script_path = output_dir / "_pyqgis_worker.py"
    script_path.write_text(_WORKER_SCRIPT, encoding="utf-8")

    cmd = [python_bin, str(script_path), str(qgz_path), str(output_dir), json.dumps(REPORT_NAMES)]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=_HEADLESS_ENV)

    if proc.returncode != 0:
        raise RuntimeError(f"Le script PyQGIS a echoue (code {proc.returncode}).\nstdout: {proc.stdout}\nstderr: {proc.stderr}")

    # la derniere ligne de stdout doit etre le JSON de resultats (QGIS peut
    # ecrire d'autres messages/warnings sur stdout avant)
    try:
        results = json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as exc:
        raise RuntimeError(f"Reponse du script PyQGIS illisible : {exc}\nstdout: {proc.stdout}\nstderr: {proc.stderr}")

    pdfs = {}
    errors = []
    for label, info in results.items():
        if info.get("ok"):
            pdfs[label] = Path(info["path"])
        else:
            errors.append(f"{label} : {info.get('error')}")

    if errors:
        raise RuntimeError("Echec export pour : " + " | ".join(errors))

    return pdfs
