"""Interaktywna mapa wariantu (Leaflet jako komponent Streamlit).

* zielone pinezki = załadunki, czerwone = rozładunki, linia = trasa po drogach (ciężarówka),
* przeciągnięcie fragmentu linii tworzy punkt pośredni (via); punkty można przesuwać
  (dragend) i usuwać (dwuklik / PPM); zdarzenia wracają do Pythona, który przelicza trasę i koszty.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import streamlit.components.v1 as components

from .optimizer import Variant
from .route_edit import Via
from .routing import RouteResult

_component = components.declare_component("ltl_route_map", path=str(Path(__file__).parent / "map_component"))


def simplify_shape(shape, tol_m: float = 25.0) -> list:
    """Douglas-Peucker: linia trasy z silnika ma setki tysięcy punktów, a mapa potrzebuje tysięcy.
    Zachowuje kształt z dokładnością `tol_m` metrów (na ekranie niewidoczne)."""
    pts = np.asarray(shape, float)
    n = len(pts)
    if n < 200:
        return [list(p) for p in pts]
    xy = np.column_stack([pts[:, 1] * np.cos(np.radians(pts[:, 0].mean())), pts[:, 0]]) * 111_320.0
    keep = np.zeros(n, bool)
    keep[[0, n - 1]] = True
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        if b <= a + 1:
            continue
        seg, rest = xy[b] - xy[a], xy[a + 1:b] - xy[a]
        length = float(np.hypot(*seg))
        dist = np.abs(seg[0] * rest[:, 1] - seg[1] * rest[:, 0]) / length if length > 0 else np.hypot(rest[:, 0], rest[:, 1])
        i = int(np.argmax(dist))
        if dist[i] > tol_m:
            m = a + 1 + i
            keep[m] = True
            stack += [(a, m), (m, b)]
    return [list(p) for p in pts[keep]]


def route_map(variant: Variant, route: RouteResult, vias: list[Via], *, map_id: str, fit_id: str,
              height: int = 600, key: str | None = None):
    """Rysuje mapę i zwraca ostatnie zdarzenie edycji (dict) lub None."""
    stops = [{"lat": s.lat, "lon": s.lon, "kind": s.kind,
              "label": f"{s.order_id} · {s.country} {s.postal} {s.city}".strip()} for s in variant.stops]
    if not route.has_real:
        banner = "Trasa orientacyjna - brak routingu po drogach"
    elif route.straight_legs:
        banner = "Odcinki liczone linią prostą: " + ", ".join(str(i + 1) for i in route.straight_legs)
    else:
        banner = ""
    straight = list(route.straight_legs)
    leg_info = [{"label": f"{a['kind'] == 'L' and '▲' or '▼'} {a['label'].split(' · ')[0]} → "
                          f"{b['kind'] == 'L' and '▲' or '▼'} {b['label'].split(' · ')[0]}",
                 "km": round(leg.km), "h": round(leg.hours, 1)}
                for a, b, leg in zip(stops, stops[1:], route.legs)]
    return _component(
        stops=stops, leg_info=leg_info,
        legs=[simplify_shape(leg.shape) for leg in route.legs],
        vias=[{"id": v.id, "lat": v.lat, "lon": v.lon} for v in vias],
        banner=banner, straight=straight, map_id=map_id, fit_id=fit_id, height=height,
        key=key, default=None,
    )
