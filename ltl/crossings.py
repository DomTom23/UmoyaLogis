"""Wykrywanie na trasie: przepraw promowych / Eurotunelu (La Manche), płatnych tuneli alpejskich, Szwajcarii."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .geo import haversine_km
from .regions import (ALPINE_TOLL_TUNNELS, CHANNEL_FERRIES, EUROTUNNEL, OTHER_FERRIES, shape_in_switzerland)
from .routing import RouteResult

FERRY_ENTER, FERRY_EXIT = 28, 29      # typy manewrów Valhalla
SEA_GAP_KM = 4.0                      # przeskok między kolejnymi punktami linii = przeprawa przez wodę


@dataclass(frozen=True)
class Crossing:
    kind: str      # "channel_tunnel" | "channel_ferry" | "ferry" | "alpine_tunnel" | "switzerland"
    name: str
    leg: int = 0

    @property
    def icon(self) -> str:
        return {"channel_tunnel": "🚇", "channel_ferry": "🚢", "ferry": "⛴️", "alpine_tunnel": "🏔️",
                "switzerland": "⚠️"}[self.kind]

    @property
    def label(self) -> str:
        return f"{self.icon} {self.name}"


def _near(shape: np.ndarray, point, km: float) -> bool:
    return bool((haversine_km(shape[:, 0], shape[:, 1], point[0], point[1]) <= km).any())


def _ferry_segments(leg) -> list[tuple[tuple, tuple, bool]]:
    """Odcinki przepraw: (początek, koniec, z_manewru). Manewry promu z silnika tras są pewne; przeskoki
    linii nad wodą (fallback) uznajemy tylko, gdy pasują do znanej przeprawy (patrz detect_crossings)."""
    segs = []
    man = leg.maneuvers
    for i, m in enumerate(man):
        if m.get("type") == FERRY_ENTER:
            end = next((x["begin"] for x in man[i + 1:] if x.get("type") == FERRY_EXIT), m.get("end") or m["begin"])
            segs.append((tuple(m["begin"]), tuple(end), True))
    shape = np.asarray(leg.shape, float)
    if len(shape) > 1:  # fallback: skok linii nad wodą
        gaps = haversine_km(shape[:-1, 0], shape[:-1, 1], shape[1:, 0], shape[1:, 1])
        for k in np.nonzero(gaps > SEA_GAP_KM)[0]:
            a, b = tuple(shape[k]), tuple(shape[k + 1])
            if not any(float(haversine_km(a[0], a[1], s[0][0], s[0][1])) < 15 for s in segs):
                segs.append((a, b, False))
    return segs


def _match(a, b, table):
    best = None
    for name, p, q in table:
        d = min(float(haversine_km(*a, *p)) + float(haversine_km(*b, *q)),
                float(haversine_km(*a, *q)) + float(haversine_km(*b, *p)))
        if best is None or d < best[0]:
            best = (d, name)
    return best if best and best[0] < 90 else None


def detect_crossings(route: RouteResult) -> list[Crossing]:
    found: dict[tuple, Crossing] = {}

    def add(kind, name, leg):
        found.setdefault((kind, name), Crossing(kind, name, leg))

    for i, leg in enumerate(route.legs):
        if len(leg.shape) < 2 or i in route.straight_legs:   # linie proste nie mówią nic o drogach
            continue
        shape = np.asarray(leg.shape, float)
        names = " ".join(n.lower() for m in leg.maneuvers for n in m.get("names", [])) + " " + \
            " ".join(m.get("instruction", "").lower() for m in leg.maneuvers)

        # Eurotunnel: nazwa w manewrach albo trasa przy obu terminalach
        if any(k in names for k in ("eurotunnel", "channel tunnel", "le shuttle")) or \
                (_near(shape, EUROTUNNEL[1], 8) and _near(shape, EUROTUNNEL[2], 8)):
            add("channel_tunnel", EUROTUNNEL[0], i)
        else:
            for a, b, from_maneuver in _ferry_segments(leg):
                hit = _match(a, b, CHANNEL_FERRIES)
                if hit:
                    add("channel_ferry", hit[1], i)
                    continue
                hit = _match(a, b, OTHER_FERRIES)
                if hit:
                    add("ferry", hit[1], i)
                elif from_maneuver:
                    add("ferry", f"prom ({a[0]:.2f}, {a[1]:.2f}) → ({b[0]:.2f}, {b[1]:.2f})", i)

        for name, keywords, center, radius in ALPINE_TOLL_TUNNELS:
            if any(k in names for k in keywords) or (center and _near(shape, center, radius)):
                add("alpine_tunnel", name, i)

        if leg.via_switzerland or shape_in_switzerland(leg.shape):
            add("switzerland", "trasa przebiega przez Szwajcarię", i)
    return list(found.values())
