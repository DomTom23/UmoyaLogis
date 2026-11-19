"""Punkty dodatkowe trasy: obowiązkowy przystanek, a opcjonalnie własny ładunek zbierany w tym punkcie.

Wiersz bez ładunku to zwykły przystanek (Stop typu "W"). Wiersz z ładunkiem (LDM i/lub waga) i miejscem zrzutu
staje się własnym zleceniem „P<n>” (odbiór w punkcie, zrzut w podanym miejscu), które musi być w każdym wariancie.
Cena jest opcjonalna (domyślnie 0 - własny towar, który tylko generuje koszt).
"""
from __future__ import annotations

from typing import Callable, Iterable, Optional

import pandas as pd

from .geo import ROAD_FACTOR, haversine_km
from .optimizer import Stop

MAX_LDM, MAX_T = 13.6, 24.0


def _f(x) -> Optional[float]:
    try:
        v = float(str(x).replace(",", ".").strip())
    except (TypeError, ValueError):
        return None
    return None if v != v or v <= 0 else v


def _s(x) -> str:
    return "" if x is None or (isinstance(x, float) and x != x) else str(x).strip()


def build_extra_points(rows: Iterable[dict], geocode: Callable[[str, str], tuple]
                       ) -> tuple[list[Stop], pd.DataFrame, list[str]]:
    """rows: country, postal, desc, ldm, weight, price, drop_country, drop_postal, drop_desc.
    geocode(country, postal) -> (lat, lon, źródło). Zwraca (przystanki bez ładunku, własne zlecenia, uwagi)."""
    stops, own, notes, n = [], [], [], 0
    for r in rows:
        postal = _s(r.get("postal"))
        if not postal:
            continue
        n += 1
        tag = f"P{n}"
        country = (_s(r.get("country")) or "PL").upper()
        if len(country) != 2:
            notes.append(f"Punkt {tag}: podaj dwuliterowy kod kraju.")
            n -= 1
            continue
        lat, lon, src = geocode(country, postal)
        if src == "kraj":
            notes.append(f"Punkt {tag} ({country} {postal}): nie udało się zlokalizować kodu - użyto środka kraju.")
        desc = _s(r.get("desc"))
        ldm, wt, price = _f(r.get("ldm")), _f(r.get("weight")), _f(r.get("price"))
        has_cargo = ldm is not None or wt is not None
        drop_postal = _s(r.get("drop_postal"))
        if has_cargo and not drop_postal:
            notes.append(f"Punkt {tag}: ładunek wymaga miejsca zrzutu (kraj i kod) - bez niego ładunek pominięto, "
                         "punkt zostaje zwykłym przystankiem.")
            has_cargo = False
        if has_cargo:
            ldm = ldm if ldm is not None else min(MAX_LDM, wt / MAX_T * MAX_LDM)
            wt = wt if wt is not None else ldm / MAX_LDM * MAX_T
            if ldm > MAX_LDM + 1e-9 or wt > MAX_T + 1e-9:
                notes.append(f"Punkt {tag}: ładunek ({ldm:g} LDM, {wt:g} t) nie mieści się na naczepie "
                             f"({MAX_LDM:g} LDM / {MAX_T:g} t) - pominięto, punkt zostaje zwykłym przystankiem.")
                has_cargo = False
        if not has_cargo:
            stops.append(Stop(order_id=tag, kind="W", country=country, postal=postal, city=desc,
                              lat=float(lat), lon=float(lon)))
            continue
        dc = (_s(r.get("drop_country")) or country).upper()
        if len(dc) != 2:
            notes.append(f"Punkt {tag}: zrzut - podaj dwuliterowy kod kraju; ładunek pominięto.")
            stops.append(Stop(order_id=tag, kind="W", country=country, postal=postal, city=desc,
                              lat=float(lat), lon=float(lon)))
            continue
        dlat, dlon, dsrc = geocode(dc, drop_postal)
        if dsrc == "kraj":
            notes.append(f"Punkt {tag}, zrzut ({dc} {drop_postal}): nie udało się zlokalizować kodu - użyto środka kraju.")
        km = float(haversine_km(lat, lon, dlat, dlon)) * ROAD_FACTOR
        own.append({
            "id": tag, "source": "Własny", "start_country": country, "start_postal": postal, "start_city": desc,
            "end_country": dc, "end_postal": drop_postal, "end_city": _s(r.get("drop_desc")),
            "load_date": None, "unload_date": None, "weight_t": round(wt, 2), "ldm": round(ldm, 2),
            "distance_km": None, "price_eur": price if price is not None else 0.0,
            "client": f"własny ładunek {tag}", "load_type": "LTL", "vehicles": "", "body": "",
            "extras": "", "description": desc, "status": "Własny ładunek",
            "defaults_used": "własne dane" + ("" if price is not None else ", bez ceny"),
            "start_lat": float(lat), "start_lon": float(lon), "end_lat": float(dlat), "end_lon": float(dlon),
            "distance_est_km": round(km), "price_used_eur": price if price is not None else 0.0,
            "price_kind": "własny", "price_note": "własne dane z punktu dodatkowego", "exclusion": "",
        })
    return stops, pd.DataFrame(own), notes
