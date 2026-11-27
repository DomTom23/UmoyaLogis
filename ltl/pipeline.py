"""Etapy przetwarzania tabeli zleceń, które nie zależą od interfejsu - dzięki temu można je buforować (patrz app.py).

Każde przeładowanie strony (kliknięcie, zmiana zakładki) przechodzi cały skrypt; bez buforowania te same ceny,
kwalifikacja i duplikaty byłyby liczone od nowa mimo niezmienionych danych.
"""
from __future__ import annotations

import pandas as pd

from .duplicates import DupParams, find_duplicates, find_geo_duplicates
from .eligibility import FleetProfile, check_eligibility
from .pricing import apply_price_rules


def mark_duplicates(orders: pd.DataFrame, dup_of: dict) -> pd.DataFrame:
    """Dopisuje powód „duplikat Tn” do kolumny exclusion wierszy wskazanych w `dup_of`."""
    if dup_of:
        orders = orders.copy()
        orders["exclusion"] = [(f"{e}, " if e else "") + f"duplikat {dup_of[i]}" if i in dup_of else e
                               for i, e in zip(orders.index, orders["exclusion"])]
    return orders


def classify_orders(orders: pd.DataFrame, price_rules, use_estimates: bool, fleet: FleetProfile,
                    dup_params: DupParams) -> pd.DataFrame:
    """Ceny (z oferty / szacunkowe), kwalifikacja do naszego zestawu i duplikaty po kodach pocztowych.
    Zwraca tabelę z kolumnami ceny oraz `exclusion` (pusty = oferta wchodzi do wyszukiwania)."""
    orders = apply_price_rules(orders, price_rules, use_estimates)
    fleet_why = check_eligibility(orders, fleet)
    orders["exclusion"] = [w or ("bez ceny: " + note if kind == "brak" else "")
                           for w, kind, note in zip(fleet_why, orders["price_kind"], orders["price_note"])]
    prio = (orders["exclusion"] == "").astype(float) * 1e9 + orders["price_used_eur"].fillna(0.0)
    return mark_duplicates(orders, find_duplicates(orders, prio, dup_params))


GEO_DUP_COLUMNS = ["id", "start_lat", "start_lon", "end_lat", "end_lon", "start_geo", "end_geo",
                   "weight_t", "ldm", "load_date"]


def geo_duplicates(located: pd.DataFrame, prio: pd.Series, dup_params: DupParams) -> dict:
    """Duplikaty wg położenia na mapie; tylko potrzebne kolumny (mały klucz dla pamięci podręcznej)."""
    return find_geo_duplicates(located[GEO_DUP_COLUMNS], prio, dup_params)
