# Generateur de dossier DP

Application Streamlit qui prend un numero de projet OpenSolar et produit le
dossier DP correspondant.

## Etat actuel

- **✅ Palier 1 -- `.qgz`** : fonctionnel, pur Python (`generate_dp.py`),
  aucune dependance a QGIS a l'execution. Tient largement dans les limites
  de Streamlit Community Cloud.
- **⏸️ Palier 2 -- 6 PDF (DP1/DP2/DP4/DP6/DP7/DP8)** : squelette pret
  (`pdf_export.py`), **mais non valide** -- necessite QGIS installe sur la
  machine qui execute l'app, et l'identifiant exact de l'algorithme
  d'export a confirmer localement (voir docstring de `pdf_export.py`).

## Mise en route locale

```bash
git clone <repo>
cd dp-app
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
# remplir secrets.toml avec les identifiants OpenSolar / cle Google

streamlit run app.py
```

Place ton template QGIS nettoye (6 rapports DP1/DP2/DP4/DP6/DP7/DP8, sans
doublon d'id) dans `templates/modele_DP_v4.2.qgz`.

## Deploiement sur Streamlit Community Cloud

1. Push ce repo sur GitHub (public ou prive selon ton compte).
2. Sur [share.streamlit.io](https://share.streamlit.io), "New app" -> pointe
   vers ce repo, fichier d'entree `app.py`.
3. Dans les parametres de l'app (⋮ -> Settings -> Secrets), colle le
   contenu de ton `secrets.toml` rempli.
4. `packages.txt` sera automatiquement detecte et ses paquets installes
   (`gdal-bin` -- necessaire des le Palier 1).

## Activer le Palier 2 (export PDF) -- a faire dans cet ordre

1. Ajoute `qgis`, `python3-qgis` et `xvfb` dans `packages.txt` (un par
   ligne, sans commentaire -- Community Cloud ne supporte PAS les lignes
   `#` dans ce fichier : chaque mot de chaque ligne est installe tel quel
   par `apt-get`, commentaires compris, ce qui provoque des erreurs
   `Unable to locate package`).
2. Deploie et regarde les logs de build : QGIS est un paquet volumineux,
   verifie que le build ne timeout pas et que l'app demarre.
3. Dans un terminal sur la meme machine (ou en local avec QGIS installe),
   lance `qgis_process list | grep -i layout` pour confirmer l'identifiant
   exact de l'algorithme d'export PDF, et mets a jour `ALGORITHM_ID` dans
   `pdf_export.py` si besoin.
4. Genere un `.qgz` de test et lance `export_dp_pdfs(...)` directement en
   Python pour valider avant de decommenter l'appel dans `app.py`.
5. Si l'app depasse les limites de ressources (1 Go RAM) une fois QGIS
   charge -- **plan de repli** : deplacer uniquement cette etape (export
   PDF) vers un workflow **GitHub Actions** (runners a 7 Go de RAM, QGIS
   s'installe sans souci) declenche depuis l'app via l'API GitHub
   (`workflow_dispatch`), le `.qgz` etant pousse en artifact d'entree et les
   PDF recuperes en artifact de sortie. L'app Streamlit garde uniquement le
   role d'interface (Palier 1 + declenchement + polling du resultat).

## Points de vigilance herites du pipeline existant

- `--type` (type d'installation) n'est pas deduit automatiquement
  d'OpenSolar -- champ obligatoire dans le formulaire.
- L'etape Street View (DP7/DP8) est ignoree silencieusement si aucune
  `google_api_key` n'est fournie dans les secrets.
- Chaque generation utilise un dossier de travail temporaire isole
  (`tempfile.TemporaryDirectory()`), donc deux utilisateurs simultanes ne se
  marchent pas dessus.
