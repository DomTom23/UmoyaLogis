"""Moduł 3: wyszukiwanie najbardziej zyskownych kombinacji zleceń (doładunków LTL).

Założenia:
* trasa startuje w pierwszym załadunku, kończy się na ostatnim rozładunku (bez powrotu),
* dowolna kolejność punktów, byle załadunek zlecenia poprzedzał jego rozładunek
  (obejmuje A1->A2->B1->B2 oraz A1->B1->A2->B2),
* w żadnym momencie ładunek na naczepie nie przekracza max LDM / max tonażu,
* odchylenie (detour) = dystans trasy - dystans najdłuższego zlecenia w kombinacji;
  kombinacje powyżej limitu są odrzucane,
* odległości: haversine * 1.3 (ltl.geo).

Przycinanie: dla kombinacji z "głównym" (najdłuższym) zleceniem m każde inne zlecenie j
musi spełniać detour(m, j) <= limit. Jest to warunek konieczny (trasa dla całej kombinacji
zawiera podtrasę dla pary, a nierówność trójkąta gwarantuje, że nie jest dłuższa), więc
przycinanie nie gubi żadnego poprawnego wariantu.
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from typing import Optional

import numpy as np
import pandas as pd

from .costs import CostParams, profit
from .geo import road_distance_matrix


@dataclass(frozen=True)
class OptimizerParams:
    max_detour_km: float = 300.0
    max_ldm: float = 13.6
    max_weight_t: float = 24.0
    max_orders: int = 3              # ile zleceń w jednym wariancie (2-4)
    top_n: int = 10
    max_load_date_gap_days: Optional[int] = 3
    candidate_limit: int = 150
    max_evaluations: int = 300_000   # bezpiecznik czasu obliczeń


@dataclass
class Stop:
    order_id: str
    kind: str          # "L" załadunek | "U" rozładunek
    country: str
    postal: str
    city: str
    lat: float
    lon: float


@dataclass
class Variant:
    order_ids: list
    stops: list
    revenue: float
    cost: dict
    profit: float
    total_km: float
    loaded_km: float
    empty_km: float
    detour_km: float
    days: float
    drive_h: float

    @property
    def profit_per_day(self) -> float:
        return self.profit / self.days if self.days else 0.0


def _best_route(idx, D, ldm, wt, max_ldm, max_wt, bound=float("inf")):
    """Najkrótsza kolejność punktów (DFS z odcinaniem). Punkty zlecenia i: 2i (L), 2i+1 (U).

    Zwraca (dystans, [kolejność punktów]) lub None. `idx` to lista indeksów zleceń.
    """
    k = len(idx)
    best = [bound, None]
    order = []

    def rec(cur, dist, done_mask, picked_mask, l_ldm, l_wt):
        if dist >= best[0]:
            return
        if done_mask == (1 << k) - 1:
            best[0], best[1] = dist, order.copy()
            return
        for j in range(k):
            bit = 1 << j
            if done_mask & bit:
                continue
            o = idx[j]
            if not picked_mask & bit:  # załadunek
                n_ldm, n_wt = l_ldm + ldm[o], l_wt + wt[o]
                if n_ldm > max_ldm + 1e-9 or n_wt > max_wt + 1e-9:
                    continue
                p = 2 * o
                step = 0.0 if cur is None else D[cur, p]
                order.append(p)
                rec(p, dist + step, done_mask, picked_mask | bit, n_ldm, n_wt)
            else:  # rozładunek
                p = 2 * o + 1
                order.append(p)
                rec(p, dist + D[cur, p], done_mask | bit, picked_mask, l_ldm - ldm[o], l_wt - wt[o])
            order.pop()

    rec(None, 0.0, 0, 0, 0.0, 0.0)
    return (best[0], best[1]) if best[1] is not None else None


def _date_ok(a, b, gap) -> bool:
    if gap is None or a is None or b is None or pd.isna(a) or pd.isna(b):
        return True
    return abs((a - b).days) <= gap


def find_best_combinations(
    orders: pd.DataFrame, cost: CostParams, opt: OptimizerParams
) -> tuple[list[Variant], dict]:
    """Zwraca (TOP warianty, statystyki). `orders` musi mieć kolumny współrzędnych (ltl.geo.add_coordinates)."""
    stats = {"candidates": 0, "evaluated": 0, "truncated": False}
    df = orders[(orders["price_eur"].notna())].copy()
    df = df[(df["ldm"] <= opt.max_ldm + 1e-9) & (df["weight_t"] <= opt.max_weight_t + 1e-9)]
    if len(df) < 2:
        return [], stats
    df = df.reset_index(drop=True)

    n = len(df)
    lats = np.empty(2 * n)
    lons = np.empty(2 * n)
    lats[0::2], lons[0::2] = df["start_lat"].to_numpy(float), df["start_lon"].to_numpy(float)
    lats[1::2], lons[1::2] = df["end_lat"].to_numpy(float), df["end_lon"].to_numpy(float)
    D = road_distance_matrix(lats, lons)
    single = np.array([D[2 * i, 2 * i + 1] for i in range(n)])
    if n > opt.candidate_limit:  # zostaw zlecenia o najwyższej cenie za km
        score = df["price_eur"].to_numpy(float) / np.maximum(single, 50.0)
        keep = np.sort(np.argsort(-score)[: opt.candidate_limit])
        df = df.iloc[keep].reset_index(drop=True)
        n = len(df)
        lats, lons = lats.reshape(-1, 2)[keep].ravel(), lons.reshape(-1, 2)[keep].ravel()
        D = road_distance_matrix(lats, lons)
        single = np.array([D[2 * i, 2 * i + 1] for i in range(n)])
    stats["candidates"] = n

    ldm, wt = df["ldm"].to_numpy(float), df["weight_t"].to_numpy(float)
    price = df["price_eur"].to_numpy(float)
    ldates = list(df["load_date"])
    max_k = max(2, min(opt.max_orders, 4))

    # "główne" = najdłuższe; rozstrzyganie remisów indeksem
    rank = sorted(range(n), key=lambda i: (-single[i], i))
    pos = {i: r for r, i in enumerate(rank)}

    compat: dict[int, list[int]] = {}
    for m in range(n):
        lst = []
        for j in range(n):
            if j == m or pos[j] < pos[m]:  # j musi być krótsze od m
                continue
            if not _date_ok(ldates[m], ldates[j], opt.max_load_date_gap_days):
                continue
            r = _best_route([m, j], D, ldm, wt, opt.max_ldm, opt.max_weight_t)
            if r and r[0] - single[m] <= opt.max_detour_km:
                lst.append(j)
        compat[m] = lst

    results: list[tuple[float, tuple, float, list]] = []
    seen_eval = 0
    for m in range(n):
        if stats["truncated"]:
            break
        cand = compat[m]
        for size in range(1, max_k):
            for sub in combinations(cand, size):
                if seen_eval >= opt.max_evaluations:
                    stats["truncated"] = True
                    break
                seen_eval += 1
                idx = [m, *sub]
                if any(not _date_ok(ldates[a], ldates[b], opt.max_load_date_gap_days)
                       for a, b in combinations(idx, 2)):
                    continue
                r = _best_route(idx, D, ldm, wt, opt.max_ldm, opt.max_weight_t)
                if not r:
                    continue
                total, seq = r
                detour = total - single[m]
                if detour > opt.max_detour_km:
                    continue
                revenue = float(sum(price[i] for i in idx))
                pr = profit(revenue, total, 2 * len(idx), cost)["profit"]
                results.append((pr, tuple(idx), total, seq))
            if stats["truncated"]:
                break
    stats["evaluated"] = seen_eval

    results.sort(key=lambda t: -t[0])
    variants = [_build_variant(df, D, single, r, cost) for r in results[: opt.top_n]]
    return variants, stats


def _build_variant(df, D, single, res, cost: CostParams) -> Variant:
    pr, idx, total, seq = res
    stops, loaded, empty = [], 0.0, 0.0
    on_board = set()
    for step, p in enumerate(seq):
        i, is_unload = p // 2, bool(p % 2)
        row = df.iloc[i]
        side = "end" if is_unload else "start"
        stops.append(Stop(
            order_id=row["id"], kind="U" if is_unload else "L",
            country=row[f"{side}_country"], postal=row[f"{side}_postal"], city=row[f"{side}_city"],
            lat=float(row[f"{side}_lat"]), lon=float(row[f"{side}_lon"]),
        ))
        if step > 0:
            leg = D[seq[step - 1], p]
            if on_board:
                loaded += leg
            else:
                empty += leg
        on_board.add(i) if not is_unload else on_board.discard(i)
    revenue = float(sum(df.iloc[i]["price_eur"] for i in idx))
    c = profit(revenue, total, len(seq), cost)
    main = max(single[i] for i in idx)
    return Variant(
        order_ids=[df.iloc[i]["id"] for i in idx], stops=stops, revenue=revenue, cost=c,
        profit=c["profit"], total_km=total, loaded_km=loaded, empty_km=empty,
        detour_km=total - main, days=c["days"], drive_h=c["drive_h"],
    )


def variants_to_frame(variants: list[Variant]) -> pd.DataFrame:
    rows = []
    for rank, v in enumerate(variants, start=1):
        route = " → ".join(f"{'▲' if s.kind == 'L' else '▼'} {s.order_id} {s.country} {s.postal}" for s in v.stops)
        rows.append({
            "#": rank,
            "Zlecenia": " + ".join(v.order_ids),
            "Trasa": route,
            "Przychód (EUR)": round(v.revenue, 0),
            "Koszty (EUR)": round(v.cost["total"], 0),
            "Zysk (EUR)": round(v.profit, 0),
            "Zysk/dzień (EUR)": round(v.profit_per_day, 0),
            "Czas trwania (dni)": round(v.days, 1),
            "Czas jazdy (h)": round(v.drive_h, 1),
            "Dystans pełny (km)": round(v.loaded_km, 0),
            "Dystans pusty (km)": round(v.empty_km, 0),
            "Odchylenie (km)": round(v.detour_km, 0),
        })
    return pd.DataFrame(rows)
