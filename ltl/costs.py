"""Moduł 2: koszty operacyjne i marża."""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class CostParams:
    day_cost_eur: float = 300.0        # auto + kierowca za dzień pracy
    avg_speed_kmh: float = 65.0
    fuel_l_per_100km: float = 30.0
    fuel_price_eur: float = 1.55
    toll_eur_per_km: float = 0.20
    max_drive_h_day: float = 9.0       # limit jazdy na dobę
    max_drive_h_week: float = 56.0     # limit jazdy na tydzień
    handling_h_per_stop: float = 1.0   # załadunek / rozładunek
    weekly_rest_days: float = 2.0      # ~45 h odpoczynku po wyczerpaniu 56 h
    channel_crossing_eur: float = 0.0  # opłata za Eurotunel / prom przez La Manche (za jedną przeprawę)
    alpine_tunnel_eur: float = 0.0     # opłata za płatny tunel alpejski (za jeden tunel)


def trip_time(distance_km: float, n_stops: int, p: CostParams) -> dict:
    """Czas jazdy i liczba dni pracy z uwzględnieniem pauz.

    Dzień pracy = max_drive_h_day godzin (jazda + załadunki). Po każdych 56 h jazdy
    doliczany jest tygodniowy odpoczynek (płatny jak dzień pracy: auto stoi, kierowca jest w kosztach).
    """
    drive_h = distance_km / p.avg_speed_kmh
    work_h = drive_h + n_stops * p.handling_h_per_stop
    work_days = max(1, math.ceil(work_h / p.max_drive_h_day - 1e-9))
    weekly_rests = max(0, math.ceil(drive_h / p.max_drive_h_week - 1e-9) - 1)
    days = work_days + weekly_rests * p.weekly_rest_days
    return {"drive_h": drive_h, "work_h": work_h, "days": days}


def operating_cost(distance_km: float, n_stops: int, p: CostParams) -> dict:
    t = trip_time(distance_km, n_stops, p)
    labor = t["days"] * p.day_cost_eur
    fuel = distance_km * (p.fuel_l_per_100km / 100.0) * p.fuel_price_eur
    tolls = distance_km * p.toll_eur_per_km
    return {**t, "labor": labor, "fuel": fuel, "tolls": tolls, "total": labor + fuel + tolls}


def profit(revenue_eur: float, distance_km: float, n_stops: int, p: CostParams) -> dict:
    c = operating_cost(distance_km, n_stops, p)
    c["revenue"] = revenue_eur
    c["profit"] = revenue_eur - c["total"]
    return c
