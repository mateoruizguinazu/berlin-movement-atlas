# Berlin Movement Atlas

Convierte un export de **Google Maps Timeline** en dos visualizaciones de por dónde te moviste:

- **`heatmap_berlin.html`** — mapa de calor con [folium](https://python-visualization.github.io/folium/), animación mensual y capa de lugares frecuentes.
- **`berlin_explorer.html`** — explorador con [deck.gl](https://deck.gl) + [MapLibre](https://maplibre.org): heatmap, hexágonos 3D, grilla 3D, puntos y **recorrido animado**, con filtros por hora, día de semana y fuente del dato.

Ambos se generan en local. **Ningún dato sale de tu máquina** salvo las peticiones de tiles del mapa base.

---

## Privacidad

Los archivos generados **llevan tus coordenadas embebidas**, incluidas las de casa y trabajo — el export de Google las trae etiquetadas como `HOME` y `WORK`. El `.gitignore` de este repo excluye el export, los datos intermedios y los HTML generados.

Si alguna vez commiteás uno por error, borrarlo en un commit posterior **no alcanza**: queda en el historial. Hay que reescribir la historia con `git filter-repo` y forzar el push.

---

## Uso

Descargá tu export desde [Google Takeout](https://takeout.google.com/) (Maps Timeline) o desde la propia app de Google Maps, y dejá el archivo como `timeline.json` en la raíz.

```bash
python3 -m venv .venv
./.venv/bin/pip install folium
./.venv/bin/python build_heatmap.py     # -> heatmap_berlin.html + berlin_points.json
./.venv/bin/python build_explorer.py    # -> berlin_explorer.html
```

`heatmap_berlin.html` abre con doble clic. Para el explorador conviene servirlo por HTTP, porque MapLibre crea *web workers* desde `blob:` y algunos navegadores los bloquean en `file://`:

```bash
python3 -m http.server 8777
```

Y abrí `http://localhost:8777/berlin_explorer.html`.

### Adaptarlo a otra ciudad

En `build_heatmap.py`:

```python
LAT_MIN, LAT_MAX = 52.34, 52.68     # bounding box
LON_MIN, LON_MAX = 13.09, 13.77
CENTER = [52.5158, 13.4540]         # vista inicial
ZOOM = 13
```

---

## El formato del export (2024 en adelante)

El export actual **no** usa el formato legacy `latitudeE7`/`longitudeE7`, así que no hay que dividir por 10.000.000. Las coordenadas son strings en grados decimales con símbolo de grado:

```json
{ "point": "52.5337766°, 13.4046677°" }
```

Y hay **cuatro fuentes distintas** de coordenadas, no una. `build_heatmap.py` las usa todas:

| Fuente | Campo | Aporta |
|---|---|---|
| `semanticSegments[].timelinePath[]` | `point` | traza densa de recorridos |
| `semanticSegments[].activity` | `start/end.latLng` | extremos + modo de transporte |
| `semanticSegments[].visit` | `topCandidate.placeLocation.latLng` | lugares **con duración de estadía** |
| `rawSignals[].position` | `LatLng` | GPS crudo + altitud + precisión |

`userLocationProfile.frequentPlaces` trae además los lugares etiquetados `HOME` / `WORK`.

Las visitas se ponderan por horas de estadía: estar 8 h en un sitio pesa más que pasar caminando.

---

## Decisiones técnicas no obvias

### Compresión del rango dinámico

Es el problema central de cualquier heatmap de historial real. Tu casa acumula miles de registros apilados contra 2 o 3 de una calle cualquiera. Con agregación `SUM` lineal, deck.gl escala el color contra el máximo y **todo salvo tu casa cae bajo el umbral: el mapa aparece vacío**.

La solución tiene dos partes: pre-agregar a celdas de ~11 m y comprimir con logaritmo. Ojo con el detalle: al normalizar por día hay que escalar (`DENS_K`) antes del `log1p`, porque con argumentos menores que 1 el logaritmo es prácticamente lineal y la compresión desaparece.

### Escalas congeladas en las vistas 3D

`colorScaleType:'quantile'` recalcula los cortes en cada cuadro. Durante una animación eso hace parpadear los hexágonos y, peor, vuelve imposible comparar un mes con otro. Al arrancar la animación se muestrean varias posiciones del cursor y se fijan los dominios de color y elevación.

Probado y descartado: `elevationScaleType:'quantile'` aplana la jerarquía (todo mide igual, un bosque ilegible) y `colorScaleType:'quantize'` sobre los logs deja casi todo azul.

### Transición temporal

El cursor es fraccional y avanza por *delta-time* en `requestAnimationFrame`, no a saltos con `setInterval`. Cada día entra y sale con un `smoothstep`, así los puntos se desvanecen en vez de parpadear. El fade de entrada se aplica **solo mientras reproduce**: en pausa o tras un scrub, el borde nuevo es justo lo que querés ver entero.

### Mapas base

Tres intentos hasta dar con uno que funcione:

1. **CartoDB `dark_matter`** — ahora exige API key y estampa "API KEY REQUIRED" sobre cada tile.
2. **`tile.openstreetmap.org`** — devuelve `403 Access blocked`. Los servidores de OSM los mantienen voluntarios y su política de uso no contempla una app que al animar pide cientos de tiles por minuto. Abierto por `file://` el navegador no manda `Referer` y el bloqueo es inmediato.
3. **Los que quedaron**: [OpenFreeMap](https://openfreemap.org) (vectorial, sin API key ni límite) para el explorador y Esri Dark Gray (ráster, sin CORS) para el folium.

El estilo vectorial se descarga y se le pasa a MapLibre **ya resuelto como objeto**. Con una URL, deck.gl se engancha a un mapa todavía sin estilo y el overlay procesa las capas una sola vez y queda congelado.

### Reconstrucción de trayectos

El modo Recorrido encadena los puntos ordenados por tiempo y corta el trayecto tras **75 min** sin señal o ante un salto de más de **5 km** — un GPS que reaparece a 5 km sin nada en medio es ruido, no un recorrido. Las visitas quedan fuera: son estadías, no desplazamiento.

---

## Estructura

```
build_heatmap.py        # parsea el export, filtra por bbox, genera el folium
                        # y exporta berlin_points.json
build_explorer.py       # inyecta los datos en el template -> berlin_explorer.html
explorer_template.html  # el explorador deck.gl (UI, capas, animación)
```
