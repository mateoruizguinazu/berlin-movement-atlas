#!/usr/bin/env python3
"""
Procesa el export de Google Maps Timeline (formato `semanticSegments`, 2024+)
y genera un mapa de calor interactivo de Berlín con folium.

Formato de coordenadas en este export:
    "52.5337766°, 13.4046677°"   -> string, grados decimales, con símbolo de grado.
    (NO es el formato legacy `latitudeE7`/`longitudeE7`, que requeriría dividir por 1e7)

Fuentes de coordenadas extraídas:
    1. semanticSegments[].timelinePath[].point          -> traza densa de recorridos
    2. semanticSegments[].activity.start/end.latLng     -> extremos de desplazamientos
    3. semanticSegments[].visit.topCandidate.*.latLng   -> lugares visitados (con duración)
    4. rawSignals[].position.LatLng                     -> GPS crudo
"""

import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

import folium
from folium.plugins import HeatMap, HeatMapWithTime, Fullscreen, MiniMap

# --------------------------------------------------------------------------
# Configuración
# --------------------------------------------------------------------------
INPUT_FILE = Path("timeline.json")
OUTPUT_FILE = Path("heatmap_berlin.html")
DATA_EXPORT = Path("berlin_points.json")  # payload para la versión avanzada

# Bounding box de Berlín + periferia
LAT_MIN, LAT_MAX = 52.34, 52.68
LON_MIN, LON_MAX = 13.09, 13.77

# Vista inicial: Friedrichshain
CENTER = [52.5158, 13.4540]
ZOOM = 13

# Regex: captura dos números decimales (con signo opcional) separados por coma,
# ignorando el símbolo de grado y espacios.
COORD_RE = re.compile(r"(-?\d+\.?\d*)\s*°?\s*,\s*(-?\d+\.?\d*)\s*°?")


def parse_latlng(raw):
    """'52.5337766°, 13.4046677°' -> (52.5337766, 13.4046677). None si no parsea."""
    if not raw or not isinstance(raw, str):
        return None
    m = COORD_RE.search(raw)
    if not m:
        return None
    try:
        return float(m.group(1)), float(m.group(2))
    except ValueError:
        return None


def in_berlin(lat, lon):
    """True si la coordenada cae dentro del bounding box de Berlín."""
    return LAT_MIN <= lat <= LAT_MAX and LON_MIN <= lon <= LON_MAX


def parse_time(raw):
    """ISO8601 con offset -> datetime. None si falla."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Extracción
# --------------------------------------------------------------------------
def extract_points(data):
    """
    Devuelve (points, stats).
    points: lista de dicts {lat, lon, t (ISO), src, weight, kind}
    """
    points = []
    stats = Counter()

    # --- 1 y 2 y 3: semanticSegments ---
    for seg in data.get("semanticSegments", []):
        start = seg.get("startTime")
        end = seg.get("endTime")

        # 1. timelinePath: la traza más densa
        for node in seg.get("timelinePath", []) or []:
            c = parse_latlng(node.get("point"))
            stats["timelinePath_total"] += 1
            if not c:
                stats["unparsed"] += 1
                continue
            if not in_berlin(*c):
                stats["outside_bbox"] += 1
                continue
            points.append({
                "lat": c[0], "lon": c[1],
                "t": node.get("time") or start,
                "src": "path", "weight": 1.0, "kind": "move",
            })
            stats["timelinePath_kept"] += 1

        # 2. activity: extremos del desplazamiento + modo de transporte
        act = seg.get("activity")
        if act:
            mode = (act.get("topCandidate") or {}).get("type", "UNKNOWN")
            dist = act.get("distanceMeters", 0.0)
            for side, tstamp in (("start", start), ("end", end)):
                c = parse_latlng((act.get(side) or {}).get("latLng"))
                stats["activity_total"] += 1
                if not c:
                    stats["unparsed"] += 1
                    continue
                if not in_berlin(*c):
                    stats["outside_bbox"] += 1
                    continue
                points.append({
                    "lat": c[0], "lon": c[1], "t": tstamp,
                    "src": "activity", "weight": 1.0, "kind": mode,
                    "dist": dist,
                })
                stats["activity_kept"] += 1

        # 3. visit: lugares donde te quedaste. El peso es la duración en horas,
        #    porque estar 8h en un lugar "pesa" más que pasar caminando por él.
        vis = seg.get("visit")
        if vis:
            top = vis.get("topCandidate") or {}
            c = parse_latlng((top.get("placeLocation") or {}).get("latLng"))
            stats["visit_total"] += 1
            if not c:
                stats["unparsed"] += 1
            elif not in_berlin(*c):
                stats["outside_bbox"] += 1
            else:
                t0, t1 = parse_time(start), parse_time(end)
                hours = (t1 - t0).total_seconds() / 3600 if t0 and t1 else 0.5
                hours = max(0.05, min(hours, 24.0))  # acotado: evita outliers
                points.append({
                    "lat": c[0], "lon": c[1], "t": start,
                    "src": "visit", "weight": hours,
                    "kind": top.get("semanticType", "UNKNOWN"),
                    "hours": round(hours, 2),
                    "placeId": top.get("placeId"),
                })
                stats["visit_kept"] += 1

    # --- 4. rawSignals: GPS crudo ---
    for sig in data.get("rawSignals", []):
        pos = sig.get("position")
        if not pos:
            continue
        stats["rawSignal_total"] += 1
        c = parse_latlng(pos.get("LatLng"))
        if not c:
            stats["unparsed"] += 1
            continue
        if not in_berlin(*c):
            stats["outside_bbox"] += 1
            continue
        # Descarta lecturas con precisión pobre (>100 m) para no emborronar el mapa
        if (pos.get("accuracyMeters") or 0) > 100:
            stats["low_accuracy"] += 1
            continue
        points.append({
            "lat": c[0], "lon": c[1],
            "t": pos.get("timestamp"),
            "src": "raw", "weight": 1.0, "kind": "gps",
            "alt": pos.get("altitudeMeters"),
            "speed": pos.get("speedMetersPerSecond"),
        })
        stats["rawSignal_kept"] += 1

    return points, stats


def extract_places(data):
    """Lugares frecuentes etiquetados (HOME / WORK / ...) dentro de Berlín."""
    out = []
    for p in (data.get("userLocationProfile") or {}).get("frequentPlaces", []):
        c = parse_latlng(p.get("placeLocation"))
        if c and in_berlin(*c):
            out.append({
                "lat": c[0], "lon": c[1],
                "label": p.get("label", "FREQUENT"),
                "placeId": p.get("placeId"),
            })
    return out


# --------------------------------------------------------------------------
# Agregación a grilla (evita que folium colapse con ~100k puntos)
# --------------------------------------------------------------------------
def grid_aggregate(points, precision=4):
    """
    Agrupa puntos en una grilla redondeando a `precision` decimales.
    precision=4 -> celdas de ~11 m. Suma pesos por celda.
    """
    cells = {}
    for p in points:
        key = (round(p["lat"], precision), round(p["lon"], precision))
        cells[key] = cells.get(key, 0.0) + p["weight"]
    return [[lat, lon, w] for (lat, lon), w in cells.items()]


# --------------------------------------------------------------------------
# Construcción del mapa
# --------------------------------------------------------------------------
# Historial del mapa base, para que no se repita:
#   1. CartoDB dark_matter -> ahora exige API key y estampa "API KEY REQUIRED".
#   2. tile.openstreetmap.org -> devuelve 403 "Access blocked". Los servidores
#      de OSM los mantienen voluntarios y su política de uso no contempla este
#      tipo de app; además, abierto con doble clic (file://) el navegador no
#      manda Referer y el bloqueo es inmediato.
#   3. Esri Dark Gray Canvas -> raster, sin API key, ya viene en gris oscuro
#      (no hace falta invertir nada por CSS) y al cargarse como <img> no
#      necesita CORS, así que funciona abriendo el archivo directamente.
ESRI = "https://services.arcgisonline.com/ArcGIS/rest/services/Canvas"
BASE_TILES = ESRI + "/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}"
LABEL_TILES = ESRI + "/World_Dark_Gray_Reference/MapServer/tile/{z}/{y}/{x}"
ESRI_ATTR = "Tiles &copy; Esri — Esri, DeLorme, NAVTEQ"

# El basemap ya es oscuro: sólo se ajusta el fondo del contenedor y el estilo
# de la atribución. Nada de grayscale/invert, que lo volverían claro.
DARK_BASEMAP_CSS = """
<style>
  .leaflet-container { background: #0a0c10; }
  /* Esri "Dark Gray" es en realidad un gris medio: se oscurece para que el
     heatmap contraste. Sólo la primera capa (la base); los rótulos van en la
     segunda y se dejan intactos para que sigan leyéndose. */
  .leaflet-tile-pane .leaflet-layer:first-child { filter: brightness(.58) contrast(1.08); }
  .leaflet-control-attribution {
    background: rgba(10,12,16,.7) !important;
    color: #7c8794 !important;
  }
  .leaflet-control-attribution a { color: #9fb3c8 !important; }
</style>
"""


def build_map(heat_data, places, points):
    m = folium.Map(
        location=CENTER,
        zoom_start=ZOOM,
        tiles=None,
        control_scale=True,
    )
    m.get_root().header.add_child(folium.Element(DARK_BASEMAP_CSS))
    folium.TileLayer(
        tiles=BASE_TILES, attr=ESRI_ATTR,
        name="Berlín (gris oscuro)", control=False, max_zoom=16,
    ).add_to(m)
    # capa de rótulos separada: va por encima del heatmap para no perderla
    folium.TileLayer(
        tiles=LABEL_TILES, attr=ESRI_ATTR,
        name="Rótulos", overlay=True, control=True, max_zoom=16,
    ).add_to(m)

    # Gradiente frío->cálido, legible sobre fondo oscuro
    gradient = {
        0.0: "#0b1d51",
        0.3: "#1b6ca8",
        0.5: "#20c997",
        0.7: "#ffd166",
        0.9: "#f7734a",
        1.0: "#ff2e63",
    }

    HeatMap(
        heat_data,
        name="Mapa de calor",
        radius=11,
        blur=14,
        min_opacity=0.25,
        max_zoom=15,
        gradient=gradient,
    ).add_to(m)

    # Capa de lugares frecuentes (HOME / WORK)
    fg = folium.FeatureGroup(name="Lugares frecuentes", show=True)
    for pl in places:
        label = pl["label"]
        color = {"HOME": "#20c997", "WORK": "#ffd166"}.get(label, "#8899aa")
        folium.CircleMarker(
            location=[pl["lat"], pl["lon"]],
            radius=7 if label in ("HOME", "WORK") else 4,
            color=color,
            weight=2,
            fill=True,
            fill_opacity=0.15,
            tooltip=label.title(),
        ).add_to(fg)
    fg.add_to(m)

    # Animación temporal por mes (capa opcional, apagada por defecto)
    monthly = {}
    for p in points:
        if not p.get("t"):
            continue
        month = p["t"][:7]
        monthly.setdefault(month, []).append([p["lat"], p["lon"]])
    months = sorted(monthly)
    if months:
        # Submuestreo: HeatMapWithTime se vuelve pesado con demasiados puntos/frame
        frames = [monthly[mo][:1500] for mo in months]
        HeatMapWithTime(
            frames,
            index=months,
            name="Animación mensual",
            radius=13,
            gradient=gradient,
            min_opacity=0.2,
            max_opacity=0.85,
            auto_play=False,
            display_index=True,
            overlay=True,
        ).add_to(m)

    Fullscreen(position="topright").add_to(m)
    # MiniMap recibe el parámetro `tile_layer`, no `tiles`: pasarle tiles=... lo
    # manda a **kwargs, se ignora en silencio y el minimapa se queda con el OSM
    # por defecto (que es justo el que está bloqueado).
    MiniMap(
        tile_layer=folium.TileLayer(tiles=BASE_TILES, attr=ESRI_ATTR, max_zoom=16),
        toggle_display=True, position="bottomright",
    ).add_to(m)
    folium.LayerControl(collapsed=False).add_to(m)

    # Rectángulo del bounding box usado como filtro
    folium.Rectangle(
        bounds=[[LAT_MIN, LON_MIN], [LAT_MAX, LON_MAX]],
        color="#ffffff", weight=1, opacity=0.18, fill=False,
        tooltip="Bounding box Berlín",
    ).add_to(m)

    return m


# --------------------------------------------------------------------------
def main():
    print(f"Leyendo {INPUT_FILE} ...")
    with INPUT_FILE.open(encoding="utf-8") as fh:
        data = json.load(fh)

    points, stats = extract_points(data)
    places = extract_places(data)

    total_seen = (stats["timelinePath_total"] + stats["activity_total"]
                  + stats["visit_total"] + stats["rawSignal_total"])

    print("\n--- Extracción ---")
    print(f"  Coordenadas encontradas : {total_seen:,}")
    print(f"  Dentro de Berlín        : {len(points):,}")
    print(f"  Fuera del bounding box  : {stats['outside_bbox']:,}  (descartadas)")
    print(f"  Precisión baja (>100 m) : {stats['low_accuracy']:,}  (descartadas)")
    print(f"  No parseables           : {stats['unparsed']:,}")
    print("  Por fuente:")
    for src in ("timelinePath", "activity", "visit", "rawSignal"):
        print(f"    {src:<13}: {stats[src + '_kept']:>7,} / {stats[src + '_total']:,}")
    print(f"  Lugares etiquetados     : {len(places)}  "
          f"({', '.join(sorted({p['label'] for p in places}))})")

    if not points:
        raise SystemExit("No quedaron puntos dentro del bounding box. Revisá el filtro.")

    times = sorted(p["t"][:10] for p in points if p.get("t"))
    print(f"  Rango temporal          : {times[0]} -> {times[-1]}")

    heat_data = grid_aggregate(points)
    print(f"\n  Celdas de grilla (~11 m): {len(heat_data):,}")

    print(f"\nGenerando {OUTPUT_FILE} ...")
    m = build_map(heat_data, places, points)
    m.save(str(OUTPUT_FILE))
    size_mb = OUTPUT_FILE.stat().st_size / 1e6
    print(f"  Listo: {OUTPUT_FILE}  ({size_mb:.1f} MB)")

    export_payload(points, places)


# --------------------------------------------------------------------------
# Payload compacto para el explorador deck.gl
# --------------------------------------------------------------------------
SRC_CODES = {"path": 0, "activity": 1, "visit": 2, "raw": 3}


def export_payload(points, places):
    """
    Serializa los puntos como arrays planos paralelos (no como lista de objetos):
    ~4x más compacto en JSON y se carga directo a TypedArrays en el navegador.
    """
    epoch = None
    rows = []
    for p in points:
        dt = parse_time(p.get("t"))
        if dt is None:
            continue
        rows.append((p, dt))
    if not rows:
        raise SystemExit("Ningún punto tiene timestamp parseable.")

    epoch = min(dt.date() for _, dt in rows)
    rows.sort(key=lambda r: r[1])

    xs, ys, days, hours, dows, ws, srcs, mins = [], [], [], [], [], [], [], []
    for p, dt in rows:
        xs.append(round(p["lon"], 5))
        ys.append(round(p["lat"], 5))
        d = (dt.date() - epoch).days
        days.append(d)
        hours.append(dt.hour)
        dows.append(dt.weekday())          # 0 = lunes
        # minutos desde epoch: el modo Recorrido necesita ordenar y cortar
        # trayectos con resolución fina; día + hora no alcanza.
        mins.append(d * 1440 + dt.hour * 60 + dt.minute)
        ws.append(round(p["weight"], 2))
        srcs.append(SRC_CODES.get(p["src"], 0))

    payload = {
        "epoch": epoch.isoformat(),
        "nDays": days[-1] + 1,
        "n": len(xs),
        "x": xs, "y": ys, "day": days, "hour": hours,
        "dow": dows, "w": ws, "src": srcs, "tmin": mins,
        "places": places,
        "bbox": [LAT_MIN, LON_MIN, LAT_MAX, LON_MAX],
        "center": CENTER,
    }
    with DATA_EXPORT.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"))
    print(f"  Datos exportados: {DATA_EXPORT}  "
          f"({DATA_EXPORT.stat().st_size / 1e6:.1f} MB, "
          f"{len(xs):,} puntos, epoch {epoch})")


if __name__ == "__main__":
    main()
