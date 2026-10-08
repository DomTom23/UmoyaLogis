"""Estymacja współrzędnych kodów pocztowych i odległości drogowych (MVP).

* Współrzędne: opcjonalnie online (api.zippopotam.us), z cache na dysku;
  gdy brak sieci / kodu - środek ciężkości kraju (źródło oznaczone "kraj").
* Odległość: linia prosta (haversine) * ROAD_FACTOR (1.3).
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

ROAD_FACTOR = 1.3
EARTH_RADIUS_KM = 6371.0088

COUNTRY_CENTROIDS = {
    "AL": (41.15, 20.17), "AT": (47.52, 14.55), "BA": (43.92, 17.68), "BE": (50.50, 4.47),
    "BG": (42.73, 25.49), "BY": (53.71, 27.95), "CH": (46.82, 8.23), "CY": (35.13, 33.43),
    "CZ": (49.82, 15.47), "DE": (51.17, 10.45), "DK": (56.26, 9.50), "EE": (58.60, 25.01),
    "ES": (40.46, -3.75), "FI": (61.92, 25.75), "FR": (46.23, 2.21), "GB": (54.00, -2.00),
    "GR": (39.07, 21.82), "HR": (45.10, 15.20), "HU": (47.16, 19.50), "IE": (53.41, -8.24),
    "IS": (64.96, -19.02), "IT": (42.50, 12.57), "LI": (47.17, 9.56), "LT": (55.17, 23.88),
    "LU": (49.82, 6.13), "LV": (56.88, 24.60), "MA": (31.79, -7.09), "MD": (47.41, 28.37),
    "ME": (42.71, 19.37), "MK": (41.61, 21.75), "MT": (35.94, 14.38), "NL": (52.13, 5.29),
    "NO": (60.47, 8.47), "PL": (51.92, 19.15), "PT": (39.40, -8.22), "RO": (45.94, 24.97),
    "RS": (44.02, 21.01), "RU": (55.75, 37.62), "SE": (60.13, 18.64), "SI": (46.15, 14.99),
    "SK": (48.67, 19.70), "TR": (38.96, 35.24), "UA": (48.38, 31.17), "XK": (42.60, 20.90),
}

DEFAULT_CACHE = Path.home() / ".cache" / "umoyalogis" / "geocache.json"


def haversine_km(lat1, lon1, lat2, lon2):
    """Odległość w linii prostej; działa na skalarach i tablicach numpy."""
    lat1, lon1, lat2, lon2 = map(np.radians, (lat1, lon1, lat2, lon2))
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_KM * np.arcsin(np.sqrt(a))


def road_distance_matrix(lats, lons, factor: float = ROAD_FACTOR) -> np.ndarray:
    lats, lons = np.asarray(lats, float), np.asarray(lons, float)
    return haversine_km(lats[:, None], lons[:, None], lats[None, :], lons[None, :]) * factor


class Geocoder:
    """Kod pocztowy -> (lat, lon, źródło). Źródło: 'online' | 'cache' | 'kraj'."""

    def __init__(self, online: bool = True, cache_path: Optional[Path] = DEFAULT_CACHE, timeout: float = 3.0):
        self.online = online
        self.timeout = timeout
        self.cache_path = Path(cache_path) if cache_path else None
        self._cache: dict[str, list] = {}
        self._dirty = False
        if self.cache_path and self.cache_path.exists():
            try:
                self._cache = json.loads(self.cache_path.read_text())
            except (OSError, ValueError):
                self._cache = {}

    @staticmethod
    def _query_code(country: str, postal: str) -> str:
        postal = postal.strip().upper()
        if country == "GB":
            return postal.split()[0]
        return postal.replace(" ", "")

    def _fetch(self, country: str, code: str) -> Optional[tuple[float, float]]:
        url = f"https://api.zippopotam.us/{country.lower()}/{code}"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                place = json.load(resp)["places"][0]
            return float(place["latitude"]), float(place["longitude"])
        except urllib.error.HTTPError:
            return None  # kod nieznany - to nie awaria sieci
        except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError):
            self.online = False  # brak sieci: nie męczymy się kolejnymi timeoutami
            return None

    def locate(self, country: str, postal: str) -> tuple[float, float, str]:
        code = self._query_code(country, postal)
        key = f"{country}|{code}"
        if key in self._cache:
            lat, lon = self._cache[key]
            return lat, lon, "cache"
        if self.online and re.fullmatch(r"[A-Z0-9\-]{2,10}", code):
            hit = self._fetch(country, code)
            if hit:
                self._cache[key] = list(hit)
                self._dirty = True
                return hit[0], hit[1], "online"
        lat, lon = COUNTRY_CENTROIDS.get(country, (50.0, 10.0))
        return lat, lon, "kraj"

    def save(self) -> None:
        if self.cache_path and self._dirty:
            try:
                self.cache_path.parent.mkdir(parents=True, exist_ok=True)
                self.cache_path.write_text(json.dumps(self._cache))
                self._dirty = False
            except OSError:
                pass


def add_coordinates(df: pd.DataFrame, geocoder: Geocoder) -> pd.DataFrame:
    """Dodaje start_lat/lon, end_lat/lon oraz geo_precise (czy oba punkty z kodu pocztowego)."""
    df = df.copy()
    for side in ("start", "end"):
        res = [geocoder.locate(c, p) for c, p in zip(df[f"{side}_country"], df[f"{side}_postal"])]
        df[f"{side}_lat"] = [r[0] for r in res]
        df[f"{side}_lon"] = [r[1] for r in res]
        df[f"{side}_geo"] = [r[2] for r in res]
    df["geo_precise"] = (df["start_geo"] != "kraj") & (df["end_geo"] != "kraj")
    geocoder.save()
    return df
