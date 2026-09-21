#!/usr/bin/env python3
"""
Inyecta berlin_points.json dentro de explorer_template.html y produce
berlin_explorer.html: un único archivo autocontenido (salvo CDN + tiles).

Requiere haber corrido antes build_heatmap.py.
"""

from pathlib import Path

TEMPLATE = Path("explorer_template.html")
DATA = Path("berlin_points.json")
OUT = Path("berlin_explorer.html")


def main():
    for f in (TEMPLATE, DATA):
        if not f.exists():
            raise SystemExit(f"Falta {f}. Corré primero: python build_heatmap.py")

    html = TEMPLATE.read_text(encoding="utf-8")
    payload = DATA.read_text(encoding="utf-8")

    # El payload vive dentro de <script type="application/json">. Hay que
    # neutralizar cualquier "</script>" literal (no aparece en datos numéricos,
    # pero sí podría colarse en un placeId).
    payload = payload.replace("</", "<\\/")

    if "__PAYLOAD__" not in html:
        raise SystemExit("El template no contiene el marcador __PAYLOAD__.")

    OUT.write_text(html.replace("__PAYLOAD__", payload), encoding="utf-8")
    print(f"Listo: {OUT}  ({OUT.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
