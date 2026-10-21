"""Dane geograficzne do analizy tras: Szwajcaria (omijana), przeprawy przez kanał La Manche, płatne tunele alpejskie.

Współrzędne są przybliżone (granica Szwajcarii ~10 km dokładności, tunele ~kilka km) - wystarczają do
wykrywania przebiegu trasy i ostrzegania użytkownika, nie do nawigacji.
"""
from __future__ import annotations

import numpy as np

from .geo import haversine_km

# --- Szwajcaria: uproszczony obrys (lat, lon), zgodnie z ruchem wskazówek zegara od Bazylei
SWITZERLAND = [
    (47.59, 7.59), (47.56, 7.80), (47.62, 8.22), (47.65, 8.45), (47.78, 8.55), (47.80, 8.62),
    (47.72, 8.80), (47.66, 8.87), (47.66, 9.17), (47.55, 9.45), (47.47, 9.62), (47.38, 9.65),
    (47.17, 9.48), (47.05, 9.50), (47.00, 9.60), (46.95, 9.88), (46.88, 10.12), (46.95, 10.40),
    (46.85, 10.48), (46.63, 10.46), (46.55, 10.15), (46.30, 10.15), (46.23, 10.17), (46.33, 9.52),
    (46.30, 9.25), (46.10, 9.05), (45.83, 9.03), (45.86, 8.92), (46.00, 8.85), (46.12, 8.70),
    (46.17, 8.45), (46.20, 8.10), (45.94, 7.87), (45.87, 7.17), (46.03, 6.92), (46.39, 6.80),
    (46.38, 6.55), (46.30, 6.20), (46.13, 6.05), (46.25, 5.96), (46.40, 6.08), (46.55, 6.20),
    (46.77, 6.40), (46.95, 6.60), (47.06, 6.72), (47.23, 6.99), (47.50, 7.00), (47.42, 7.25),
]


def in_switzerland(lat, lon) -> np.ndarray:
    """Test punkt-w-wielokącie (ray casting), wektorowo dla tablic współrzędnych."""
    lat, lon = np.atleast_1d(np.asarray(lat, float)), np.atleast_1d(np.asarray(lon, float))
    py = np.array([p[0] for p in SWITZERLAND])
    px = np.array([p[1] for p in SWITZERLAND])
    inside = np.zeros(lat.shape, bool)
    j = len(px) - 1
    for i in range(len(px)):
        cond = ((py[i] > lat) != (py[j] > lat)) & (
            lon < (px[j] - px[i]) * (lat - py[i]) / (py[j] - py[i] + 1e-12) + px[i])
        inside ^= cond
        j = i
    return inside


def shape_in_switzerland(shape) -> bool:
    if not shape:
        return False
    arr = np.asarray(shape, float)
    return bool(in_switzerland(arr[:, 0], arr[:, 1]).any())


def switzerland_exclusion_polygon() -> list:
    """Obrys w formacie [[lon, lat], ...] dla opcji exclude_polygons serwera Valhalla."""
    ring = [[lon, lat] for lat, lon in SWITZERLAND]
    return [ring + [ring[0]]]


# --- punkty pośrednie do objazdu Szwajcarii (miasta na głównych trasach wokół Alp)
DETOUR_VIAS = [
    ("Lyon", 45.76, 4.84), ("Chambéry", 45.57, 5.92), ("Fréjus / Modane", 45.20, 6.66),
    ("Mont Blanc / Courmayeur", 45.80, 6.97), ("Turyn", 45.07, 7.69), ("Mediolan", 45.46, 9.19),
    ("Werona", 45.44, 10.99), ("Brenner / Bolzano", 46.50, 11.35), ("Innsbruck", 47.26, 11.39),
    ("Monachium", 48.14, 11.58), ("Stuttgart", 48.78, 9.18), ("Salzburg", 47.80, 13.04),
    ("Villach", 46.62, 13.85), ("Lubljana", 46.06, 14.51),
]


def detour_candidates(a, b, max_stretch: float = 1.7, limit: int = 4) -> list[tuple[str, float, float]]:
    """Punkty pośrednie dla odcinka a->b (dicts lat/lon), od najmniej nadkładających drogi."""
    direct = float(haversine_km(a["lat"], a["lon"], b["lat"], b["lon"])) or 1.0
    scored = []
    for name, lat, lon in DETOUR_VIAS:
        if min(float(haversine_km(a["lat"], a["lon"], lat, lon)), float(haversine_km(b["lat"], b["lon"], lat, lon))) < 60:
            continue  # punkt pośredni nie może leżeć przy końcach odcinka
        via = float(haversine_km(a["lat"], a["lon"], lat, lon)) + float(haversine_km(lat, lon, b["lat"], b["lon"]))
        if via / direct <= max_stretch:
            scored.append((via / direct, name, lat, lon))
    scored.sort()
    return [(n, la, lo) for _, n, la, lo in scored[:limit]]


# --- przeprawy przez kanał La Manche (nazwa, punkt A, punkt B)
EUROTUNNEL = ("Eurotunnel Le Shuttle (Calais–Folkestone)", (50.927, 1.804), (51.093, 1.133))
CHANNEL_FERRIES = [
    ("prom Calais–Dover", (50.967, 1.853), (51.127, 1.333)),
    ("prom Dunkierka–Dover", (51.036, 2.366), (51.127, 1.333)),
    ("prom Dieppe–Newhaven", (49.933, 1.083), (50.790, 0.056)),
    ("prom Le Havre–Portsmouth", (49.483, 0.108), (50.800, -1.100)),
    ("prom Caen (Ouistreham)–Portsmouth", (49.283, -0.250), (50.800, -1.100)),
    ("prom Cherbourg–Poole", (49.650, -1.620), (50.714, -1.987)),
    ("prom Cherbourg–Portsmouth", (49.650, -1.620), (50.800, -1.100)),
    ("prom Saint-Malo–Portsmouth", (48.645, -2.026), (50.800, -1.100)),
    ("prom Roscoff–Plymouth", (48.724, -3.985), (50.365, -4.143)),
]
OTHER_FERRIES = [
    ("prom Zeebrugge–Hull", (51.333, 3.200), (53.740, -0.330)),
    ("prom Rotterdam–Hull", (51.950, 4.050), (53.740, -0.330)),
    ("prom Hoek van Holland–Harwich", (51.980, 4.130), (51.945, 1.290)),
]

# --- płatne tunele alpejskie: (nazwa, słowa kluczowe w nazwach dróg, środek (lat, lon) lub None, promień km)
ALPINE_TOLL_TUNNELS = [
    ("Tunel Mont Blanc (FR–IT)", ["mont blanc", "mont-blanc", "monte bianco"], (45.8667, 6.90), 6.0),
    ("Tunel Fréjus (FR–IT)", ["fréjus", "frejus", "fréjus", "t4 tunnel"], (45.14, 6.68), 7.0),
    ("Tunel Tauern (A10, AT)", ["tauerntunnel", "tauern tunnel"], (47.17, 13.52), 8.0),
    ("Tunel Katschberg (A10, AT)", ["katschbergtunnel", "katschberg tunnel"], (47.05, 13.60), 6.0),
    ("Tunel Arlberg (S16, AT)", ["arlbergtunnel", "arlberg tunnel", "arlberg schnellstraße"], (47.13, 10.21), 7.0),
    ("Tunel Karawanki (A11, AT–SI)", ["karawankentunnel", "predor karavanke", "karawanken"], (46.50, 14.045), 5.0),
    ("Tunel Gleinalm (A9, AT)", ["gleinalmtunnel"], None, 0.0),
    ("Tunel Bosruck (A9, AT)", ["bosrucktunnel"], None, 0.0),
    ("Tunel Pfänder (A14, AT)", ["pfändertunnel", "pfaendertunnel"], None, 0.0),
]
