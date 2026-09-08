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

import glob
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPORT_NAMES = ["DP 1", "DP 2", "DP 4", "DP 6", "DP 7", "DP 8"]

# QGIS/Qt a besoin d'un affichage meme en mode "headless". Plutot que
# d'installer un serveur X virtuel (xvfb), on force le plugin Qt
# "offscreen", qui ne necessite aucune dependance systeme supplementaire.
# Sans effet sur Windows (variable simplement ignoree) -- l'affichage y
# est deja disponible normalement.
_HEADLESS_ENV = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}

# Candidats a essayer, dans l'ordre : le 'python3' de Streamlit (resolu via
# PATH) n'est PAS forcement celui ou l'apt-get de python3-qgis a installe
# ses bindings (Community Cloud execute l'app dans un venv separe du
# python3 systeme) -- on essaie donc explicitement les emplacements
# habituels de l'interpreteur systeme Debian avant de se rabattre sur
# 'python3' simple.
_PYTHON_CANDIDATES = [
    "/usr/bin/python3",
    "/usr/bin/python3.11",
    "/usr/bin/python3.12",
    "python3",
]


def _windows_python_candidates() -> list:
    """Sur Windows, l'interpreteur avec les bindings QGIS est celui fourni
    PAR l'installation QGIS elle-meme (pas le Python "normal" installe a
    cote) -- ni sur le PATH, ni au meme endroit que la commande ogr2ogr.
    On privilegie 'python-qgis*.bat' (script qui configure les variables
    d'environnement necessaires -- GDAL_DATA, PROJ_LIB, DLL PATH -- avant
    de lancer python3.exe) plutot que python3.exe directement, plus fiable
    sur Windows a cause de cette configuration d'environnement."""
    candidates = []
    for base in (r"C:\Program Files", r"C:\Program Files (x86)", r"C:\OSGeo4W", r"C:\OSGeo4W64"):
        for pattern in ("QGIS*\\bin\\python-qgis*.bat", "bin\\python-qgis*.bat",
                         "QGIS*\\bin\\python3.exe", "bin\\python3.exe"):
            candidates += glob.glob(os.path.join(base, pattern))
    return sorted(set(candidates), reverse=True)  # version la plus recente en premier


def _python_invocation(python_bin: str, args: list) -> list:
    """Construit la commande a executer. Un '.bat' ne peut pas etre lance
    directement par subprocess sur Windows (CreateProcess a besoin de
    l'interpreteur de commandes) -- il faut passer par 'cmd /c'."""
    if python_bin.lower().endswith(".bat"):
        return ["cmd", "/c", python_bin] + args
    return [python_bin] + args


def _find_python_with_qgis() -> str:
    candidates = list(_PYTHON_CANDIDATES)
    if sys.platform.startswith("win"):
        candidates = _windows_python_candidates() + candidates

    tried = []
    for candidate in candidates:
        exe = candidate if Path(candidate).is_absolute() else shutil.which(candidate)
        if not exe or not Path(exe).exists():
            tried.append(f"{candidate} (introuvable)")
            continue
        check = subprocess.run(
            _python_invocation(exe, ["-c", "import qgis.core"]),
            capture_output=True, text=True, env=_HEADLESS_ENV,
        )
        if check.returncode == 0:
            return exe
        tried.append(f"{exe} ({check.stderr.strip().splitlines()[-1] if check.stderr else 'echec'})")

    raise RuntimeError(
        "Aucun interpreteur Python avec le module 'qgis' trouve. Essaye : " + " | ".join(tried) +
        ". Verifie que 'python3-qgis' est bien installe (packages.txt) et localise le bon binaire "
        "(ex: 'dpkg -L python3-qgis | grep site-packages' dans un shell sur la meme machine). "
        "Sous Windows, verifie que QGIS Desktop est bien installe (le fichier "
        "'python-qgis.bat' ou 'python-qgis-ltr.bat' doit exister dans le dossier bin\\ de l'installation)."
    )

_WORKER_SCRIPT = """
import sys, json
from qgis.core import QgsApplication, QgsProject, QgsLayoutExporter, QgsReport, QgsRasterLayer

qgz_path, output_dir, report_names_json = sys.argv[1], sys.argv[2], sys.argv[3]
report_names = json.loads(report_names_json)

qgs = QgsApplication([], False)
qgs.initQgis()

project = QgsProject.instance()
if not project.read(qgz_path):
    print(json.dumps({"error": f"Impossible d'ouvrir le projet : {qgz_path}"}))
    qgs.exitQgis()
    sys.exit(1)

# Diagnostic des couches raster (WMS notamment) -- imprime sur stdout avec
# le prefixe DIAG: pour etre facilement repere dans les logs Streamlit
# Cloud. Version durcie : la premiere tentative n'a produit AUCUNE ligne
# DIAG dans les logs de production (meme apres redemarrage complet et
# execution confirmee -- les lignes Overpass du MEME run, elles,
# apparaissaient bien). Cause exacte inconnue -- flush() explicite ajoute
# par precaution, et un marqueur de depart imprime AVANT la boucle pour
# distinguer "la boucle ne s'exécute pas du tout" de "elle s'execute mais
# ne trouve aucune couche raster".
all_layers = list(project.mapLayers().values())
print(f"DIAG: {len(all_layers)} couche(s) dans le projet", flush=True)
for layer in all_layers:
    try:
        is_raster = isinstance(layer, QgsRasterLayer)
        print(f"DIAG: '{layer.name()}' type={type(layer).__name__} raster={is_raster}", flush=True)
        if is_raster:
            err = layer.error()
            err_summary = err.summary() if not err.isEmpty() else ""
            provider = layer.dataProvider()
            prov_valid = provider.isValid() if provider else None
            print(f"DIAG:   isValid={layer.isValid()} providerValid={prov_valid} error={err_summary!r}", flush=True)
    except Exception as exc:
        print(f"DIAG: erreur pendant le diagnostic de la couche : {type(exc).__name__}: {exc}", flush=True)

# Les Rapports (QgsReport) ET les mises en page classiques (QgsPrintLayout)
# vivent tous les deux dans layoutManager() -- il n'y a pas de
# reportManager() separe dans cette version de QGIS.
manager = project.layoutManager()
available_names = [item.name() for item in manager.layouts()]

results = {}
for name in report_names:
    label = name.replace(" ", "")
    obj = manager.layoutByName(name)
    if obj is None:
        results[label] = {"ok": False, "error": f"'{name}' introuvable (elements presents : {available_names})"}
        continue

    out_pdf = f"{output_dir}/{label}.pdf"
    settings = QgsLayoutExporter.PdfExportSettings()

    try:
        if isinstance(obj, QgsReport):
            # Rapport : export via la methode statique prenant un iterateur
            result, error_msg = QgsLayoutExporter.exportToPdf(obj, out_pdf, settings)
        else:
            # Mise en page classique : export via une instance de l'exporteur
            exporter = QgsLayoutExporter(obj)
            result = exporter.exportToPdf(out_pdf, settings)
            error_msg = ""

        if int(result) == 0:  # Success vaut toujours 0
            results[label] = {"ok": True, "path": out_pdf}
        else:
            results[label] = {"ok": False, "error": str(error_msg) or f"code erreur {result}"}
    except Exception as exc:
        results[label] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

qgs.exitQgis()
print(json.dumps(results), flush=True)
"""


def export_dp_pdfs(qgz_path: Path, output_dir: Path, python_bin: str = None) -> dict:
    qgz_path = Path(qgz_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    python_bin = python_bin or _find_python_with_qgis()

    script_path = output_dir / "_pyqgis_worker.py"
    script_path.write_text(_WORKER_SCRIPT, encoding="utf-8")

    cmd = _python_invocation(python_bin, [str(script_path), str(qgz_path), str(output_dir), json.dumps(REPORT_NAMES)])
    proc = subprocess.run(cmd, capture_output=True, text=True, env=_HEADLESS_ENV)

    # les lignes "DIAG:" (diagnostic des couches raster/WMS) precedent le
    # JSON final -- affichees systematiquement (succes ou echec) pour
    # apparaitre dans les logs Streamlit Cloud. Fait AVANT le parsing JSON
    # et AVANT le controle du returncode : si le worker crashe malgre tout,
    # on veut quand meme voir ce qui a ete imprime avant le crash.
    diag_lines = [l for l in proc.stdout.splitlines() if l.startswith("DIAG:")]
    if diag_lines:
        for line in diag_lines:
            print(f"  [pdf_export] {line}")
    else:
        print("  [pdf_export] (aucune ligne DIAG recue -- stdout brut ci-dessous)")
        print(f"  [pdf_export] stdout complet : {proc.stdout!r}")

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
