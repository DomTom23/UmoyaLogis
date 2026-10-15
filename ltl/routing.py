"""Trasy rzeczywiste po drogach dostępnych dla ciężarówek.

Dostawcy:
* ``valhalla`` - publiczny serwer FOSSGIS (profil ``truck``, bez klucza; fair use) lub własny,
* ``ors``      - OpenRouteService (profil ``driving-hgv``, wymaga klucza API),
* ``fallback`` - proste odcinki (haversine * 1.3), gdy routing jest niedostępny. Wynik jest
  wtedy oznaczony ``is_real = False``.

Trasa jest liczona dla listy punktów: ``break`` (załadunki/rozładunki) i ``through``
(punkty pośrednie przeciągnięte na mapie).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .geo import ROAD_FACTOR, haversine_km

VALHALLA_URL = os.environ.get("VALHALLA_URL", "https://valhalla1.openstreetmap.de")
ORS_URL = "https://api.openrouteservice.org"


@dataclass(frozen=True)
class TruckProfile:
    gross_weight_t: float = 40.0
    axle_load_t: float = 11.5
    height_m: float = 4.0
    width_m: float = 2.55
    length_m: float = 16.5
    hazmat: bool = False


@dataclass
class Leg:
    """Odcinek między dwoma kolejnymi punktami zatrzymania (załadunek/rozładunek)."""
    shape: list            # [(lat, lon), ...]
    km: float
    hours: float


@dataclass
class RouteResult:
    legs: list = field(default_factory=list)
    provider: str = ""
    is_real: bool = False
    error: str = ""

    @property
    def km(self) -> float:
        return sum(leg.km for leg in self.legs)

    @property
    def hours(self) -> float:
        return sum(leg.hours for leg in self.legs)


class RoutingError(RuntimeError):
    pass


# ----------------------------------------------------------- polyline

def decode_polyline(encoded: str, precision: int = 6) -> list[tuple[float, float]]:
    """Dekoder 'encoded polyline' (Valhalla: precision=6, ORS/Google: 5). Zwraca (lat, lon)."""
    factor = 10 ** precision
    coords, idx, lat, lon = [], 0, 0, 0
    while idx < len(encoded):
        for is_lon in (False, True):
            shift = result = 0
            while True:
                b = ord(encoded[idx]) - 63
                idx += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if is_lon:
                lon += delta
            else:
                lat += delta
        coords.append((lat / factor, lon / factor))
    return coords


def encode_polyline(coords, precision: int = 6) -> str:
    factor, out, plat, plon = 10 ** precision, [], 0, 0
    for lat, lon in coords:
        for cur, prev in ((round(lat * factor), plat), (round(lon * factor), plon)):
            v = cur - prev
            v = ~(v << 1) if v < 0 else v << 1
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1F)) + 63))
                v >>= 5
            out.append(chr(v + 63))
        plat, plon = round(lat * factor), round(lon * factor)
    return "".join(out)


# ----------------------------------------------------------- providers

def _post(url: str, payload: dict, headers: dict | None = None, timeout: float = 25.0) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "UmoyaLogis-LTL/1.0", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:200]
        raise RoutingError(f"HTTP {e.code}: {detail}") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise RoutingError(str(e)) from e


def route_valhalla(points: list[dict], truck: TruckProfile, base_url: str = VALHALLA_URL) -> RouteResult:
    """points: [{'lat', 'lon', 'type': 'break'|'through'}]; pierwszy i ostatni muszą być 'break'."""
    payload = {
        "locations": [{"lat": p["lat"], "lon": p["lon"], "type": p.get("type", "break")} for p in points],
        "costing": "truck",
        "costing_options": {"truck": {
            "weight": truck.gross_weight_t, "axle_load": truck.axle_load_t, "height": truck.height_m,
            "width": truck.width_m, "length": truck.length_m, "hazmat": truck.hazmat,
        }},
        "directions_options": {"units": "kilometers"},
    }
    data = _post(base_url.rstrip("/") + "/route", payload)
    try:
        legs = [Leg(shape=decode_polyline(leg["shape"], 6),
                    km=float(leg["summary"]["length"]), hours=float(leg["summary"]["time"]) / 3600.0)
                for leg in data["trip"]["legs"]]
    except (KeyError, TypeError) as e:
        raise RoutingError(f"nieoczekiwana odpowiedź Valhalla: {str(data)[:200]}") from e
    return RouteResult(legs=legs, provider="Valhalla (truck)", is_real=True)


def route_ors(points: list[dict], truck: TruckProfile, api_key: str) -> RouteResult:
    """OpenRouteService driving-hgv. Odcinki liczone osobno dla kolejnych punktów 'break'."""
    groups, cur = [], [points[0]]
    for p in points[1:]:
        cur.append(p)
        if p.get("type", "break") == "break":
            groups.append(cur)
            cur = [p]
    legs = []
    for g in groups:
        payload = {
            "coordinates": [[p["lon"], p["lat"]] for p in g],
            "units": "km",
            "options": {"profile_params": {"restrictions": {
                "weight": truck.gross_weight_t, "axleload": truck.axle_load_t, "height": truck.height_m,
                "width": truck.width_m, "length": truck.length_m}}},
        }
        data = _post(f"{ORS_URL}/v2/directions/driving-hgv", payload, {"Authorization": api_key})
        try:
            r = data["routes"][0]
            legs.append(Leg(shape=decode_polyline(r["geometry"], 5), km=float(r["summary"]["distance"]),
                            hours=float(r["summary"]["duration"]) / 3600.0))
        except (KeyError, IndexError, TypeError) as e:
            raise RoutingError(f"nieoczekiwana odpowiedź ORS: {str(data)[:200]}") from e
    return RouteResult(legs=legs, provider="OpenRouteService (HGV)", is_real=True)


def route_straight(points: list[dict], avg_speed_kmh: float = 65.0, error: str = "") -> RouteResult:
    """Fallback: linie proste, długość = haversine * 1.3 (trasa orientacyjna, nie po drogach)."""
    legs, cur = [], [points[0]]
    for p in points[1:]:
        cur.append(p)
        if p.get("type", "break") == "break":
            km = sum(float(haversine_km(a["lat"], a["lon"], b["lat"], b["lon"])) * ROAD_FACTOR
                     for a, b in zip(cur, cur[1:]))
            legs.append(Leg(shape=[(q["lat"], q["lon"]) for q in cur], km=km, hours=km / avg_speed_kmh))
            cur = [p]
    return RouteResult(legs=legs, provider="linia prosta ×1,3 (brak routingu)", is_real=False, error=error)


def compute_route(points: list[dict], truck: TruckProfile, provider: str = "valhalla",
                  ors_key: str = "", valhalla_url: str = VALHALLA_URL,
                  avg_speed_kmh: float = 65.0) -> RouteResult:
    """Liczy trasę wybranym dostawcą; przy błędzie zwraca fallback z opisem błędu."""
    try:
        if provider == "ors":
            if not ors_key:
                raise RoutingError("brak klucza API OpenRouteService")
            return route_ors(points, truck, ors_key)
        if provider == "valhalla":
            return route_valhalla(points, truck, valhalla_url)
    except RoutingError as e:
        return route_straight(points, avg_speed_kmh, error=str(e))
    return route_straight(points, avg_speed_kmh)
