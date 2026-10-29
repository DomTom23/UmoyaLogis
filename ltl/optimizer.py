"""Moduł 3: wyszukiwanie najbardziej zyskownych kombinacji zleceń (doładunków LTL).

Założenia:
* trasa startuje w pierwszym załadunku, kończy się na ostatnim rozładunku (bez powrotu),
* dowolna kolejność punktów, byle załadunek zlecenia poprzedzał jego rozładunek
  (obejmuje A1->A2->B1->B2 oraz A1->B1->A2->B2),
* w żadnym momencie ładunek na naczepie nie przekracza max LDM / max tonażu; liczba zleceń nie jest
  ograniczona - program dokłada kolejne zlecenia, dopóki mieszczą się na naczepie i w limicie odchylenia
  (wyszukiwanie wiązkowe z wstawianiem punktów w najtańsze miejsce trasy),
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
    top_n: int = 10
    beam_width: int = 12             # ile najlepszych częściowych wariantów rozwijać na każdym poziomie
    hard_cap_orders: int = 6         # maks. liczba zleceń w jednym wariancie
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
    peak_ldm: float = 0.0
    peak_weight_t: float = 0.0

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


def _insert(seq, j, D, ldm, wt, max_ldm, max_wt):
    """Najtańsze dołożenie zlecenia j (punkty 2j, 2j+1) do istniejącej kolejności `seq`.

    Zwraca (nowa kolejność, przyrost dystansu) albo None, gdy nie mieści się na naczepie.
    Kolejność jest sprawdzana dla każdej pozycji załadunku i rozładunku (załadunek przed rozładunkiem).
    """
    L = len(seq)
    la_l, la_w = [0.0], [0.0]
    for p in seq:
        sign = 1.0 if p % 2 == 0 else -1.0
        la_l.append(la_l[-1] + sign * ldm[p // 2])
        la_w.append(la_w[-1] + sign * wt[p // 2])
    P, Q = 2 * j, 2 * j + 1
    dl, dw = ldm[j], wt[j]

    def d(a, b):
        return 0.0 if a is None or b is None else float(D[a, b])

    best = None
    for i in range(L + 1):
        prev_i = seq[i - 1] if i > 0 else None
        next_i = seq[i] if i < L else None
        pd_ = d(prev_i, P) + d(P, next_i) - d(prev_i, next_i)
        run_l = run_w = -1e18
        for k in range(i, L + 1):
            run_l, run_w = max(run_l, la_l[k]), max(run_w, la_w[k])
            if run_l + dl > max_ldm + 1e-9 or run_w + dw > max_wt + 1e-9:
                break
            if k == i:
                delta = d(prev_i, P) + d(P, Q) + d(Q, next_i) - d(prev_i, next_i)
            else:
                prev_k, next_k = seq[k - 1], (seq[k] if k < L else None)
                delta = pd_ + d(prev_k, Q) + d(Q, next_k) - d(prev_k, next_k)
            if best is None or delta < best[0]:
                best = (delta, i, k)
    if best is None:
        return None
    delta, i, k = best
    return seq[:i] + [P] + seq[i:k] + [Q] + seq[k:], delta


def find_best_combinations(
    orders: pd.DataFrame, cost: CostParams, opt: OptimizerParams
) -> tuple[list[Variant], dict]:
    """Zwraca (TOP warianty, statystyki). `orders` musi mieć kolumny współrzędnych (ltl.geo.add_coordinates).

    Dla każdego "głównego" (najdłuższego) zlecenia m dokładamy kolejne zlecenia z puli zgodnych (limit
    odchylenia względem m): na każdym poziomie rozwijamy `beam_width` najzyskowniejszych zestawów, więc
    wypełnienie naczepy rośnie tak długo, jak zysk albo zapełnienie na to pozwalają.
    """
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

    # "główne" = najdłuższe; rozstrzyganie remisów indeksem
    rank = sorted(range(n), key=lambda i: (-single[i], i))
    pos = {i: r for r, i in enumerate(rank)}

    def pair_ok(a, b):
        return _date_ok(ldates[a], ldates[b], opt.max_load_date_gap_days)

    compat: dict[int, list[int]] = {}
    for m in range(n):
        lst = []
        for j in range(n):
            if j == m or pos[j] < pos[m] or not pair_ok(m, j):
                continue
            r = _best_route([m, j], D, ldm, wt, opt.max_ldm, opt.max_weight_t)
            if r and r[0] - single[m] <= opt.max_detour_km:
                lst.append(j)
        compat[m] = lst

    def score(idx, total):
        return profit(float(sum(price[i] for i in idx)), total, 2 * len(idx), cost)["profit"]

    best: dict[frozenset, tuple] = {}     # zestaw -> (zysk, indeksy, kolejność punktów, dystans)
    evals = 0
    for m in range(n):
        if stats["truncated"]:
            break
        frontier = {frozenset([m]): ([2 * m, 2 * m + 1], float(single[m]))}
        for _level in range(2, opt.hard_cap_orders + 1):
            new: dict[frozenset, tuple] = {}
            for key, (seq, total) in frontier.items():
                for j in compat[m]:
                    if j in key or not all(pair_ok(j, x) for x in key):
                        continue
                    evals += 1
                    if evals > opt.max_evaluations:
                        stats["truncated"] = True
                        break
                    ins = _insert(seq, j, D, ldm, wt, opt.max_ldm, opt.max_weight_t)
                    if ins is None:
                        continue
                    nseq, delta = ins
                    ntotal = total + delta
                    if ntotal - single[m] > opt.max_detour_km:
                        continue
                    fk = key | {j}
                    if fk in new and new[fk][1] <= ntotal:
                        continue
                    new[fk] = (nseq, ntotal)
                if stats["truncated"]:
                    break
            if not new:
                break
            scored = []
            for fk, (nseq, ntotal) in new.items():
                if len(fk) <= 3:  # małe zestawy: dokładna kolejność
                    r = _best_route(sorted(fk), D, ldm, wt, opt.max_ldm, opt.max_weight_t)
                    if r and r[0] < ntotal:
                        ntotal, nseq = r[0], r[1]
                        new[fk] = (nseq, ntotal)
                scored.append((score(fk, ntotal), fk))
            for pr, fk in scored:
                if fk not in best or best[fk][0] < pr:
                    best[fk] = (pr, tuple(sorted(fk)), new[fk][0], new[fk][1])
            scored.sort(key=lambda t: -t[0])
            frontier = {fk: new[fk] for _, fk in scored[: opt.beam_width]}
            if stats["truncated"]:
                break
    stats["evaluated"] = evals

    ranked = sorted(best.values(), key=lambda t: -t[0])[: opt.top_n]
    variants = [_build_variant(df, D, single, (pr, idx, total, seq), cost, ldm, wt)
                for pr, idx, seq, total in ranked]
    return variants, stats


def _build_variant(df, D, single, res, cost: CostParams, ldm=None, wt=None) -> Variant:
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
    peak_l = peak_w = cur_l = cur_w = 0.0
    for p in seq:
        sign = 1.0 if p % 2 == 0 else -1.0
        cur_l += sign * float(df.iloc[p // 2]["ldm"])
        cur_w += sign * float(df.iloc[p // 2]["weight_t"])
        peak_l, peak_w = max(peak_l, cur_l), max(peak_w, cur_w)
    revenue = float(sum(df.iloc[i]["price_eur"] for i in idx))
    c = profit(revenue, total, len(seq), cost)
    main = max(single[i] for i in idx)
    return Variant(
        order_ids=[df.iloc[i]["id"] for i in idx], stops=stops, revenue=revenue, cost=c,
        profit=c["profit"], total_km=total, loaded_km=loaded, empty_km=empty,
        detour_km=total - main, days=c["days"], drive_h=c["drive_h"], peak_ldm=peak_l, peak_weight_t=peak_w,
    )


def variants_to_frame(variants: list[Variant]) -> pd.DataFrame:
    rows = []
    for rank, v in enumerate(variants, start=1):
        route = " → ".join(f"{'▲' if s.kind == 'L' else '▼'} {s.order_id} {s.country} {s.postal}" for s in v.stops)
        rows.append({
            "#": rank,
            "Zlecenia": " + ".join(v.order_ids),
            "Liczba": len(v.order_ids),
            "Trasa": route,
            "Zapełnienie (LDM)": round(v.peak_ldm, 1),
            "Ładunek (t)": round(v.peak_weight_t, 1),
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
