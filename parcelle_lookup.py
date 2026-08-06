"""
parcelle_lookup.py
-------------------
Trouve la parcelle cadastrale (prefixe / section / numero / superficie)
qui contient un point donne (lon, lat WGS84), a partir du gpkg cadastre
deja telecharge par update_cadastre() dans generate_dp.py
("Données cadastrales.gpkg", couche "Parcelles").

Pas de dependance lourde (pas de shapely/fiona) : on extrait juste les
parcelles proches via ogr2ogr (-spat, filtre par boite englobante) puis on
fait le test point-dans-polygone en pur Python (ray casting, gere les trous).

Le GeoJSON source Etalab (cadastre.data.gouv.fr) est en WGS84 (EPSG:4326),
donc on interroge directement avec les memes lon/lat que geocode() renvoie
-- pas besoin de reprojection Lambert-93 ici.

Limite connue : le point utilise est l'adresse geocodee (BAN), pas le
centroide reel des panneaux. Pour l'immense majorite des maisons
individuelles c'est la meme parcelle ; en cas de parcelle allongee /
adresse ambigue, `find_parcelle` retombe sur la parcelle la plus proche
(par distance au centroide) si aucune parcelle ne contient le point
exactement, et le signale via le champ "certain": False.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Optional


def _point_in_ring(x: float, y: float, ring: list) -> bool:
    """Ray casting standard sur un anneau (liste de [lon,lat])."""
    inside = False
    n = len(ring)
    x1, y1 = ring[0]
    for i in range(1, n + 1):
        x2, y2 = ring[i % n]
        if ((y1 > y) != (y2 > y)) and (
            x < (x2 - x1) * (y - y1) / (y2 - y1 + 1e-15) + x1
        ):
            inside = not inside
        x1, y1 = x2, y2
    return inside


def _point_in_polygon(x: float, y: float, geometry: dict) -> bool:
    """Gere Polygon et MultiPolygon GeoJSON, avec trous (rings interieurs)."""
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")

    def polygon_contains(poly_rings):
        if not poly_rings:
            return False
        if not _point_in_ring(x, y, poly_rings[0]):
            return False
        # dans un trou -> exclu
        for hole in poly_rings[1:]:
            if _point_in_ring(x, y, hole):
                return False
        return True

    if gtype == "Polygon":
        return polygon_contains(coords)
    if gtype == "MultiPolygon":
        return any(polygon_contains(poly) for poly in coords)
    return False


def _ring_centroid(ring: list) -> tuple:
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    return sum(xs) / len(xs), sum(ys) / len(ys)


def _geometry_centroid(geometry: dict) -> tuple:
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if gtype == "Polygon":
        return _ring_centroid(coords[0])
    if gtype == "MultiPolygon":
        cxs, cys = [], []
        for poly in coords:
            cx, cy = _ring_centroid(poly[0])
            cxs.append(cx)
            cys.append(cy)
        return sum(cxs) / len(cxs), sum(cys) / len(cys)
    return (0.0, 0.0)


def find_parcelle(
    cadastre_gpkg: Path,
    ogr2ogr_path: str,
    lon: float,
    lat: float,
    layer_name: str = "Parcelles",
    buffer_deg: float = 0.001,
) -> Optional[dict]:
    """Renvoie {"prefixe","section","numero","superficie","certain"} ou None.

    `buffer_deg` = ~110m a cette latitude ; assez large pour couvrir une
    parcelle entiere autour du point tout en restant rapide a exporter.
    """
    tmp_geojson = Path(cadastre_gpkg).parent / "_tmp_parcelles_query.geojson"
    tmp_geojson.unlink(missing_ok=True)

    xmin, ymin = lon - buffer_deg, lat - buffer_deg
    xmax, ymax = lon + buffer_deg, lat + buffer_deg

    cmd = [
        ogr2ogr_path, "-f", "GeoJSON", str(tmp_geojson), str(cadastre_gpkg),
        layer_name, "-spat", str(xmin), str(ymin), str(xmax), str(ymax),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ogr2ogr a echoue lors de la recherche de parcelle :\n{result.stderr}")

    if not tmp_geojson.exists():
        return None

    try:
        data = json.loads(tmp_geojson.read_text(encoding="utf-8"))
    finally:
        tmp_geojson.unlink(missing_ok=True)

    features = data.get("features", [])
    if not features:
        return None

    def props_to_result(props: dict, certain: bool) -> dict:
        # Les cles etalab-cadastre standard : prefixe, section, numero, contenance
        return {
            "prefixe": (props.get("prefixe") or "").strip().lstrip("0") or "",
            "section": props.get("section") or "",
            "numero": props.get("numero") or "",
            "superficie": int(props.get("contenance") or 0),
            "certain": certain,
        }

    # 1) recherche exacte : point dans le polygone
    for feat in features:
        geom = feat.get("geometry")
        if geom and _point_in_polygon(lon, lat, geom):
            return props_to_result(feat.get("properties", {}), certain=True)

    # 2) repli : parcelle dont le centroide est le plus proche du point
    best, best_dist = None, None
    for feat in features:
        geom = feat.get("geometry")
        if not geom:
            continue
        cx, cy = _geometry_centroid(geom)
        d = (cx - lon) ** 2 + (cy - lat) ** 2
        if best_dist is None or d < best_dist:
            best_dist, best = d, feat

    if best is not None:
        return props_to_result(best.get("properties", {}), certain=False)

    return None
