"""Edycja trasy: punkty pośrednie (via) i przeliczanie kosztów po zmianie.

Wariant ma stały ciąg przystanków (załadunki/rozładunki). Użytkownik przeciąga fragment trasy
na mapie -> powstaje punkt via przypisany do odcinka (leg) między dwoma przystankami.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass

import numpy as np

from .costs import CostParams, profit
from .optimizer import Variant
from .routing import RouteResult, TruckProfile, compute_route

_ids = itertools.count(1)


@dataclass
class Via:
    id: int
    leg: int      # numer odcinka: 0 = między przystankiem 0 a 1
    lat: float
    lon: float


def new_via(leg: int, lat: float, lon: float) -> Via:
    return Via(id=next(_ids), leg=leg, lat=lat, lon=lon)


def _nearest_index(shape, lat, lon) -> int:
    arr = np.asarray(shape, float)
    return int(np.argmin((arr[:, 0] - lat) ** 2 + ((arr[:, 1] - lon) * np.cos(np.radians(lat))) ** 2))


def order_vias(vias: list[Via], route: RouteResult | None) -> list[Via]:
    """Kolejność: odcinek, potem pozycja wzdłuż aktualnej linii odcinka."""
    def key(v: Via):
        pos = 0
        if route and v.leg < len(route.legs) and route.legs[v.leg].shape:
            pos = _nearest_index(route.legs[v.leg].shape, v.lat, v.lon)
        return (v.leg, pos)
    return sorted(vias, key=key)


def build_points(variant: Variant, vias: list[Via]) -> list[dict]:
    pts = []
    for i, s in enumerate(variant.stops):
        pts.append({"lat": s.lat, "lon": s.lon, "type": "break"})
        if i < len(variant.stops) - 1:
            pts += [{"lat": v.lat, "lon": v.lon, "type": "through"} for v in vias if v.leg == i]
    return pts


def apply_edit(vias: list[Via], event: dict, route: RouteResult | None) -> list[Via]:
    """Obsługuje zdarzenia z mapy: add_via {leg, lat, lon} | move_via {id, lat, lon} | remove_via {id} | reset."""
    action = event.get("action")
    vias = [Via(**v.__dict__) for v in vias]
    if action == "add_via":
        vias.append(new_via(int(event["leg"]), float(event["lat"]), float(event["lon"])))
    elif action == "move_via":
        for v in vias:
            if v.id == int(event["id"]):
                v.lat, v.lon = float(event["lat"]), float(event["lon"])
    elif action == "remove_via":
        vias = [v for v in vias if v.id != int(event["id"])]
    elif action == "reset":
        vias = []
    return order_vias(vias, route)


def route_for_variant(variant: Variant, vias: list[Via], truck: TruckProfile, provider: str,
                      ors_key: str, valhalla_url: str, speed: float) -> RouteResult:
    return compute_route(build_points(variant, vias), truck, provider, ors_key, valhalla_url, speed)


def evaluate_route(variant: Variant, route: RouteResult, cost: CostParams) -> dict:
    """Koszty i zysk dla trasy rzeczywistej: km pełne/puste z odcinków, reszta wg CostParams."""
    loaded = empty = 0.0
    on_board: set[str] = set()

    def visit(stop):
        on_board.add(stop.order_id) if stop.kind == "L" else on_board.discard(stop.order_id)

    visit(variant.stops[0])
    for i, leg in enumerate(route.legs):
        if on_board:
            loaded += leg.km
        else:
            empty += leg.km
        visit(variant.stops[i + 1])
    c = profit(variant.revenue, route.km, len(variant.stops), cost)
    c.update(loaded_km=loaded, empty_km=empty, total_km=route.km, route_hours=route.hours)
    return c
