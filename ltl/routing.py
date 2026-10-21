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
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .geo import ROAD_FACTOR, haversine_km
from .regions import detour_candidates, shape_in_switzerland, switzerland_exclusion_polygon

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


@dataclass(frozen=True)
class RouteOptions:
    avoid_switzerland: bool = True     # trasa nie może przebiegać przez Szwajcarię


@dataclass
class Leg:
    """Odcinek między dwoma kolejnymi punktami zatrzymania (załadunek/rozładunek)."""
    shape: list            # [(lat, lon), ...]
    km: float
    hours: float
    maneuvers: list = field(default_factory=list)   # [{'type', 'names', 'instruction', 'begin', 'end'}]
    via_switzerland: bool = False                   # nie udało się ominąć Szwajcarii


@dataclass
class RouteResult:
    legs: list = field(default_factory=list)
    provider: str = ""
    is_real: bool = False          # wszystkie odcinki po drogach
    error: str = ""
    straight_legs: list = field(default_factory=list)  # numery odcinków liczonych linią prostą

    @property
    def switzerland_legs(self) -> list:
        return [i for i, leg in enumerate(self.legs) if leg.via_switzerland]

    @property
    def has_real(self) -> bool:
        return len(self.straight_legs) < len(self.legs)

    @property
    def km(self) -> float:
        return sum(leg.km for leg in self.legs)

    @property
    def hours(self) -> float:
        return sum(leg.hours for leg in self.legs)


class RoutingError(RuntimeError):
    """Błąd sieci / nieoczekiwana odpowiedź - dalsze zapytania nie mają sensu."""


class RoutingHTTPError(RoutingError):
    """Serwer odpowiedział błędem HTTP dla tego zapytania (np. za długi odcinek) - inne odcinki mogą się udać."""

    def __init__(self, msg: str, too_long: bool = False):
        super().__init__(msg)
        self.too_long = too_long


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
        too_long = "max distance" in detail or '"error_code":154' in detail.replace(" ", "")
        raise RoutingHTTPError(f"HTTP {e.code}: {detail}", too_long) from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise RoutingError(str(e)) from e


def _groups(points: list[dict]) -> list[list[dict]]:
    """Dzieli punkty na odcinki między kolejnymi punktami 'break' (przez 'through' w środku)."""
    groups, cur = [], [points[0]]
    for p in points[1:]:
        cur.append(p)
        if p.get("type", "break") == "break":
            groups.append(cur)
            cur = [p]
    return groups


def _straight_leg(group: list[dict], speed: float = 65.0) -> Leg:
    km = sum(float(haversine_km(a["lat"], a["lon"], b["lat"], b["lon"])) * ROAD_FACTOR
             for a, b in zip(group, group[1:]))
    return Leg(shape=[(q["lat"], q["lon"]) for q in group], km=km, hours=km / speed)


def _merge(a: Leg, b: Leg) -> Leg:
    return Leg(shape=a.shape + b.shape[1:], km=a.km + b.km, hours=a.hours + b.hours,
               maneuvers=a.maneuvers + b.maneuvers, via_switzerland=a.via_switzerland or b.via_switzerland)


_LEG_CACHE: dict = {}
_NO_EXCLUDE: set = set()      # serwery, które odrzuciły opcję exclude_polygons
_LOCK = threading.Lock()


def _parse_valhalla_leg(leg: dict) -> Leg:
    shape = decode_polyline(leg["shape"], 6)
    maneuvers = []
    for m in leg.get("maneuvers", []):
        bi, ei = m.get("begin_shape_index", 0), m.get("end_shape_index", 0)
        maneuvers.append({
            "type": m.get("type", 0), "names": m.get("street_names", []), "instruction": m.get("instruction", ""),
            "begin": shape[min(bi, len(shape) - 1)], "end": shape[min(ei, len(shape) - 1)],
        })
    return Leg(shape=shape, km=float(leg["summary"]["length"]), hours=float(leg["summary"]["time"]) / 3600.0,
               maneuvers=maneuvers)


def _valhalla_request(group, truck, base_url, exclude_ch: bool = False) -> Leg:
    payload = {
        "locations": [{"lat": p["lat"], "lon": p["lon"], "type": "break" if i in (0, len(group) - 1) else "through"}
                      for i, p in enumerate(group)],
        "costing": "truck",
        "costing_options": {"truck": {
            "weight": truck.gross_weight_t, "axle_load": truck.axle_load_t, "height": truck.height_m,
            "width": truck.width_m, "length": truck.length_m, "hazmat": truck.hazmat,
        }},
        "directions_options": {"units": "kilometers"},
    }
    url = base_url.rstrip("/") + "/route"
    use_exclude = exclude_ch and base_url not in _NO_EXCLUDE
    try:
        data = _post(url, {**payload, "exclude_polygons": switzerland_exclusion_polygon()} if use_exclude else payload)
    except RoutingHTTPError as e:
        if not use_exclude or e.too_long:
            raise
        if any(w in str(e).lower() for w in ("exclude", "polygon")):
            _NO_EXCLUDE.add(base_url)       # serwer nie obsługuje wyłączania obszarów
        data = _post(url, payload)          # ponów bez wyłączenia (Szwajcarię wykryjemy i ominiemy objazdem)
    try:
        legs = [_parse_valhalla_leg(leg) for leg in data["trip"]["legs"]]
    except (KeyError, TypeError) as e:
        raise RoutingError(f"nieoczekiwana odpowiedź Valhalla: {str(data)[:200]}") from e
    out = legs[0]
    for extra in legs[1:]:
        out = _merge(out, extra)
    return out


def _valhalla_leg(group, truck, base_url, exclude_ch: bool = False, depth: int = 0) -> Leg:
    """Jeden odcinek; gdy przekracza limit serwera (publiczny Valhalla: 1500 km), dzielony na połowy.

    Punkt podziału: środkowy punkt pośredni (via) odcinka albo punkt na linii prostej
    (serwer przyciąga go do najbliższej drogi; kilka prób, gdy trafi w morze / bezdroża).
    """
    try:
        return _valhalla_request(group, truck, base_url, exclude_ch)
    except RoutingHTTPError as e:
        if not e.too_long or depth >= 3:
            raise
        first_err = e
    if len(group) > 2:
        i = len(group) // 2
        splits = [(group[: i + 1], group[i:])]
    else:
        a, b = group
        splits = []
        for f in (0.5, 0.42, 0.58, 0.34, 0.66):
            mid = {"lat": a["lat"] + (b["lat"] - a["lat"]) * f, "lon": a["lon"] + (b["lon"] - a["lon"]) * f}
            splits.append(([a, mid], [mid, b]))
    for left, right in splits:
        try:
            return _merge(_valhalla_leg(left, truck, base_url, exclude_ch, depth + 1),
                          _valhalla_leg(right, truck, base_url, exclude_ch, depth + 1))
        except RoutingHTTPError:
            continue
    raise first_err


def _avoid_switzerland(group: list[dict], leg: Leg, route_fn) -> Leg:
    """Gdy odcinek przechodzi przez Szwajcarię: próbuje objazdu przez punkty pośrednie (Lyon, Mediolan,
    Monachium…) i wybiera najkrótszy wariant bez Szwajcarii. Nie udało się -> oznacza odcinek ostrzeżeniem."""
    if len(group) == 2 and not shape_in_switzerland([(g["lat"], g["lon"]) for g in group]):
        best = None
        for _, lat, lon in detour_candidates(group[0], group[1]):
            via = {"lat": lat, "lon": lon}
            try:
                cand = _merge(route_fn([group[0], via]), route_fn([via, group[1]]))
            except RoutingHTTPError:
                continue
            if not shape_in_switzerland(cand.shape) and (best is None or cand.km < best.km):
                best = cand
        if best is not None:
            return best
    leg.via_switzerland = True
    return leg


def _cached(key, fn):
    with _LOCK:
        hit = _LEG_CACHE.get(key)
    if hit is not None:
        return hit
    val = fn()
    with _LOCK:
        _LEG_CACHE[key] = val
    return val


def _leg_key(kind, base, truck, opts, group):
    return (kind, base, truck, opts, tuple((round(p["lat"], 5), round(p["lon"], 5)) for p in group))


def route_valhalla(points: list[dict], truck: TruckProfile, base_url: str = VALHALLA_URL,
                   avg_speed_kmh: float = 65.0, opts: RouteOptions = RouteOptions()) -> RouteResult:
    """points: [{'lat', 'lon', 'type': 'break'|'through'}]. Każdy odcinek liczony osobno
    (limit serwera dotyczy jednego zapytania); odcinek, którego nie da się wyznaczyć, jest linią prostą."""
    legs, failed, errors = [], [], []
    for i, group in enumerate(_groups(points)):
        try:
            def build(group=group):
                leg = _valhalla_leg(group, truck, base_url, opts.avoid_switzerland)
                if opts.avoid_switzerland and shape_in_switzerland(leg.shape):
                    leg = _avoid_switzerland(group, leg, lambda g: _valhalla_leg(g, truck, base_url, True))
                return leg
            legs.append(_cached(_leg_key("valhalla", base_url, truck, opts, group), build))
        except RoutingHTTPError as e:
            legs.append(_straight_leg(group, avg_speed_kmh))
            failed.append(i)
            errors.append(f"odcinek {i + 1}: {e}")
    if len(failed) == len(legs):
        return RouteResult(legs=legs, provider="linia prosta ×1,3 (brak routingu)", is_real=False,
                           error="; ".join(errors), straight_legs=failed)
    return RouteResult(legs=legs, provider="Valhalla (truck)", is_real=not failed,
                       error="; ".join(errors), straight_legs=failed)


def _ors_request(g, truck, api_key) -> Leg:
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
        return Leg(shape=decode_polyline(r["geometry"], 5), km=float(r["summary"]["distance"]),
                   hours=float(r["summary"]["duration"]) / 3600.0)
    except (KeyError, IndexError, TypeError) as e:
        raise RoutingError(f"nieoczekiwana odpowiedź ORS: {e}") from e


def route_ors(points: list[dict], truck: TruckProfile, api_key: str, avg_speed_kmh: float = 65.0,
              opts: RouteOptions = RouteOptions()) -> RouteResult:
    """OpenRouteService driving-hgv. Odcinki liczone osobno dla kolejnych punktów 'break'."""
    legs, failed, errors = [], [], []
    for i, g in enumerate(_groups(points)):
        def build(g=g):
            leg = _ors_request(g, truck, api_key)
            if opts.avoid_switzerland and shape_in_switzerland(leg.shape):
                leg = _avoid_switzerland(g, leg, lambda grp: _ors_request(grp, truck, api_key))
            return leg
        try:
            legs.append(_cached(_leg_key("ors", "", truck, opts, g), build))
        except RoutingHTTPError as e:
            legs.append(_straight_leg(g, avg_speed_kmh))
            failed.append(i)
            errors.append(f"odcinek {i + 1}: {e}")
    if len(failed) == len(legs):
        return RouteResult(legs=legs, provider="linia prosta ×1,3 (brak routingu)", is_real=False,
                           error="; ".join(errors), straight_legs=failed)
    return RouteResult(legs=legs, provider="OpenRouteService (HGV)", is_real=not failed,
                       error="; ".join(errors), straight_legs=failed)


def route_straight(points: list[dict], avg_speed_kmh: float = 65.0, error: str = "") -> RouteResult:
    """Fallback: linie proste, długość = haversine * 1.3 (trasa orientacyjna, nie po drogach)."""
    legs = [_straight_leg(g, avg_speed_kmh) for g in _groups(points)]
    return RouteResult(legs=legs, provider="linia prosta ×1,3 (brak routingu)", is_real=False, error=error,
                       straight_legs=list(range(len(legs))))


def compute_route(points: list[dict], truck: TruckProfile, provider: str = "valhalla",
                  ors_key: str = "", valhalla_url: str = VALHALLA_URL,
                  avg_speed_kmh: float = 65.0, opts: RouteOptions = RouteOptions()) -> RouteResult:
    """Liczy trasę wybranym dostawcą; przy błędzie zwraca fallback z opisem błędu."""
    try:
        if provider == "ors":
            if not ors_key:
                raise RoutingError("brak klucza API OpenRouteService")
            return route_ors(points, truck, ors_key, avg_speed_kmh, opts)
        if provider == "valhalla":
            return route_valhalla(points, truck, valhalla_url, avg_speed_kmh, opts)
    except RoutingError as e:
        return route_straight(points, avg_speed_kmh, error=str(e))
    return route_straight(points, avg_speed_kmh)
