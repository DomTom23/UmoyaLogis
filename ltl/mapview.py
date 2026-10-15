"""Interaktywna mapa wariantu (Leaflet jako komponent Streamlit).

* zielone pinezki = załadunki, czerwone = rozładunki, linia = trasa po drogach (ciężarówka),
* przeciągnięcie fragmentu linii tworzy punkt pośredni (via); punkty można przesuwać
  (dragend) i usuwać (dwuklik / PPM); zdarzenia wracają do Pythona, który przelicza trasę i koszty.
"""
from __future__ import annotations

from pathlib import Path

import streamlit.components.v1 as components

from .optimizer import Variant
from .route_edit import Via
from .routing import RouteResult

_component = components.declare_component("ltl_route_map", path=str(Path(__file__).parent / "map_component"))


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
    return _component(
        stops=stops,
        legs=[[list(p) for p in leg.shape] for leg in route.legs],
        vias=[{"id": v.id, "lat": v.lat, "lon": v.lon} for v in vias],
        banner=banner, straight=straight, map_id=map_id, fit_id=fit_id, height=height,
        key=key, default=None,
    )
