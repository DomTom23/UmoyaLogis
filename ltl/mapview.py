"""Mapa Folium dla wybranego wariantu: zielone = załadunek, czerwone = rozładunek."""
from __future__ import annotations

import folium

from .optimizer import Variant


def build_map(variant: Variant) -> folium.Map:
    pts = [(s.lat, s.lon) for s in variant.stops]
    m = folium.Map(location=pts[0], zoom_start=5, tiles="OpenStreetMap")
    folium.PolyLine(pts, color="#1f77b4", weight=4, opacity=0.8,
                    tooltip=f"{variant.total_km:.0f} km").add_to(m)
    for n, s in enumerate(variant.stops, start=1):
        load = s.kind == "L"
        folium.Marker(
            (s.lat, s.lon),
            popup=f"{n}. {'Załadunek' if load else 'Rozładunek'} {s.order_id}<br>{s.country} {s.postal} {s.city}",
            tooltip=f"{n}. {s.order_id} {'↑' if load else '↓'} {s.country} {s.postal}",
            icon=folium.Icon(color="green" if load else "red", icon="arrow-up" if load else "arrow-down",
                             prefix="fa"),
        ).add_to(m)
    m.fit_bounds(pts, padding=(30, 30))
    return m
