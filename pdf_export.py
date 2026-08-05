"""
pdf_export.py -- Palier 2 : export des 6 rapports (DP1, DP2, DP4, DP6, DP7,
DP8) du .qgz en PDF, via QGIS installe en mode headless (qgis_process).

STATUT : NON VALIDE. Je n'ai pas d'installation QGIS disponible pour tester
ce module -- l'identifiant exact de l'algorithme Processing a utiliser peut
varier selon la version de QGIS. A valider en local (ou sur un runner
GitHub Actions avec QGIS installe) AVANT de l'activer dans app.py :

    qgis_process list | grep -i layout

... pour trouver l'id exact (attendu : quelque chose comme
"native:printlayouttopdf" ou "qgis:printlayouttopdf" selon la version).
Une fois confirme, mettre a jour ALGORITHM_ID ci-dessous.

Usage prevu (une fois valide) :
    from pdf_export import export_dp_pdfs
    pdfs = export_dp_pdfs(qgz_path, output_dir)
    # -> {"DP1": Path(".../DP1.pdf"), "DP2": Path(...), ...}
"""

import subprocess
from pathlib import Path

REPORT_NAMES = ["DP 1", "DP 2", "DP 4", "DP 6", "DP 7", "DP 8"]

# A CONFIRMER localement (voir docstring) avant d'utiliser ce module.
ALGORITHM_ID = "native:printlayouttopdf"


def export_dp_pdfs(qgz_path: Path, output_dir: Path, qgis_process_bin: str = "qgis_process") -> dict:
    qgz_path = Path(qgz_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    results = {}
    for report_name in REPORT_NAMES:
        label = report_name.replace(" ", "")  # "DP 1" -> "DP1"
        out_pdf = output_dir / f"{label}.pdf"

        cmd = [
            qgis_process_bin, "run", ALGORITHM_ID,
            f"--PROJECT_PATH={qgz_path}",
            f"--LAYOUT={report_name}",
            f"--OUTPUT={out_pdf}",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0 or not out_pdf.exists():
            raise RuntimeError(
                f"Echec export {report_name} :\n"
                f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
            )
        results[label] = out_pdf

    return results
