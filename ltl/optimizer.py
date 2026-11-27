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

import math
from datetime import date
from dataclasses import dataclass
from itertools import combinations
from typing import Optional

import numpy as np
import pandas as pd

from .costs import CostParams, profit
from .geo import ROAD_FACTOR, haversine_km, road_distance_matrix
from .schedule import ScheduleParams, StopSpec, simulate


@dataclass(frozen=True)
class OptimizerParams:
    max_detour_km: float = 300.0
    max_ldm: float = 13.6
    max_weight_t: float = 24.0
    top_n: int = 10
    beam_width: int = 12             # ile najlepszych częściowych wariantów rozwijać na każdym poziomie
    hard_cap_orders: int = 6         # maks. liczba zleceń w jednym wariancie
    max_load_date_gap_days: Optional[int] = 3
    candidate_limit: int = 300
    max_evaluations: int = 300_000   # bezpiecznik czasu obliczeń (dzielony sprawiedliwie między zlecenia główne)
    compat_limit: int = 40           # ile najlepszych zgodnych zleceń rozważać przy jednym zleceniu głównym


@dataclass(frozen=True)
class TimeCtx:
    """Okna czasowe i zasady jazdy dla optymalizatora (None = czas nie jest brany pod uwagę)."""
    params: ScheduleParams
    windows: dict            # numer zlecenia -> {"L": (od_h, do_h) | None, "U": ...}


@dataclass
class Stop:
    order_id: str
    kind: str          # "L" załadunek | "U" rozładunek | "S" start pojazdu | "W" punkt dodatkowy (bez zlecenia)
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


def _best_route(idx, D, ldm, wt, max_ldm, max_wt, bound=float("inf"), d0=None, ok=None):
    """Najkrótsza kolejność punktów (DFS z odcinaniem). Punkty zlecenia i: 2i (L), 2i+1 (U).

    Zwraca (dystans, [kolejność punktów]) lub None. `idx` to lista indeksów zleceń. `d0` (opcjonalnie) to
    odległości od miejsca startu pojazdu do punktów - wlicza dojazd do pierwszego załadunku.
    `D` może być macierzą numpy albo listą list (ta druga jest ok. 3x szybsza przy odczycie pojedynczych komórek).
    """
    k = len(idx)
    if not isinstance(D, list):
        D = D.tolist()
    if k == 2 and bound == float("inf"):
        return _best_route_pair(idx, D, ldm, wt, max_ldm, max_wt, d0, ok)
    best = [bound, None]
    order = []
    full = (1 << k) - 1
    eps_l, eps_w = max_ldm + 1e-9, max_wt + 1e-9

    def rec(cur, dist, done_mask, picked_mask, l_ldm, l_wt):
        if dist >= best[0]:
            return
        if done_mask == full:
            if ok is not None and not ok(order):       # kolejność niemieszcząca się w oknach czasowych
                return
            best[0], best[1] = dist, order.copy()
            return
        for j in range(k):
            bit = 1 << j
            if done_mask & bit:
                continue
            o = idx[j]
            if not picked_mask & bit:  # załadunek
                n_ldm, n_wt = l_ldm + ldm[o], l_wt + wt[o]
                if n_ldm > eps_l or n_wt > eps_w:
                    continue
                p = 2 * o
                step = (0.0 if d0 is None else d0[p]) if cur is None else D[cur][p]
                order.append(p)
                rec(p, dist + step, done_mask, picked_mask | bit, n_ldm, n_wt)
            else:  # rozładunek
                p = 2 * o + 1
                order.append(p)
                rec(p, dist + D[cur][p], done_mask | bit, picked_mask, l_ldm - ldm[o], l_wt - wt[o])
            order.pop()

    rec(None, 0.0, 0, 0, 0.0, 0.0)
    return (best[0], best[1]) if best[1] is not None else None


# kolejność przeglądania sześciu możliwych układów dwóch zleceń (a = idx[0], b = idx[1]) - dokładnie taka, jak w DFS
# powyżej; L/U + zlecenie: (0, 'L') = załadunek a, (1, 'U') = rozładunek b itd.
_PAIR_ORDERS = (((0, 0), (0, 1), (1, 0), (1, 1)),
                ((0, 0), (1, 0), (0, 1), (1, 1)),
                ((0, 0), (1, 0), (1, 1), (0, 1)),
                ((1, 0), (0, 0), (0, 1), (1, 1)),
                ((1, 0), (0, 0), (1, 1), (0, 1)),
                ((1, 0), (1, 1), (0, 0), (0, 1)))


def _best_route_pair(idx, D, ldm, wt, max_ldm, max_wt, d0, ok):
    """To samo co DFS w `_best_route` dla dokładnie dwóch zleceń, bez rekurencji: sprawdza sześć układów w tej samej
    kolejności i wybiera pierwszy o najmniejszym dystansie (jak odcinanie `dist >= best` w DFS)."""
    o = (idx[0], idx[1])
    eps_l, eps_w = max_ldm + 1e-9, max_wt + 1e-9
    best_d, best_seq = float("inf"), None
    for seq in _PAIR_ORDERS:
        cur_l = cur_w = 0.0
        fits = True
        for i, u in seq:                                   # zapełnienie naczepy po każdej operacji
            if u == 0:
                cur_l, cur_w = cur_l + ldm[o[i]], cur_w + wt[o[i]]
                if cur_l > eps_l or cur_w > eps_w:
                    fits = False
                    break
            else:
                cur_l, cur_w = cur_l - ldm[o[i]], cur_w - wt[o[i]]
        if not fits:
            continue
        pts = [2 * o[i] + u for i, u in seq]
        dist = 0.0 if d0 is None else d0[pts[0]]
        for a, b in zip(pts, pts[1:]):
            dist += D[a][b]
        if dist >= best_d:
            continue
        if ok is not None and not ok(pts):
            continue
        best_d, best_seq = dist, pts
    return (best_d, best_seq) if best_seq is not None else None


def plan_len(D, seq) -> float:
    return float(sum(D[a, b] for a, b in zip(seq, seq[1:])))


def _date_ok(a, b, gap) -> bool:
    if gap is None or a is None or b is None or pd.isna(a) or pd.isna(b):
        return True
    return abs((a - b).days) <= gap


def _insert(seq, j, D, ldm, wt, max_ldm, max_wt, ok=None):
    """Najtańsze dołożenie zlecenia j (punkty 2j, 2j+1) do istniejącej kolejności `seq`.

    Zwraca (nowa kolejność, przyrost dystansu) albo None, gdy nie mieści się na naczepie.
    Kolejność jest sprawdzana dla każdej pozycji załadunku i rozładunku (załadunek przed rozładunkiem).
    Odległości łączące punkty `seq` z nowymi są liczone raz na punkt (a nie dla każdej pary pozycji).
    """
    if not isinstance(D, list):
        D = D.tolist()
    L = len(seq)
    la_l, la_w = [0.0], [0.0]
    for p in seq:
        sign = 1.0 if p % 2 == 0 else -1.0
        la_l.append(la_l[-1] + sign * ldm[p // 2])
        la_w.append(la_w[-1] + sign * wt[p // 2])
    P, Q = 2 * j, 2 * j + 1
    dl, dw = ldm[j], wt[j]
    eps_l, eps_w = max_ldm + 1e-9, max_wt + 1e-9
    DP, DQ = D[P], D[Q]
    d_pq = DP[Q]
    # d(prev, X) dla prev = seq[i-1], d(X, next) dla next = seq[i], d(prev, next); brzeg (None) = 0
    p_in = [0.0] + [D[seq[i - 1]][P] for i in range(1, L + 1)]          # d(seq[i-1], P)
    p_out = [DP[seq[i]] for i in range(L)] + [0.0]                       # d(P, seq[i])
    q_in = [0.0] + [D[seq[i - 1]][Q] for i in range(1, L + 1)]          # d(seq[i-1], Q)
    q_out = [DQ[seq[i]] for i in range(L)] + [0.0]                       # d(Q, seq[i])
    link = [0.0] + [D[seq[i - 1]][seq[i]] for i in range(1, L)] + [0.0]  # d(seq[i-1], seq[i])

    best = None
    cands = [] if ok is not None else None            # z oknami czasowymi: najtańsza POZYCJA, która się mieści w terminach
    for i in range(L + 1):
        pd_ = p_in[i] + p_out[i] - link[i]
        run_l = run_w = -1e18
        for k in range(i, L + 1):
            la = la_l[k]
            if la > run_l:
                run_l = la
            lw = la_w[k]
            if lw > run_w:
                run_w = lw
            if run_l + dl > eps_l or run_w + dw > eps_w:
                break
            if k == i:
                delta = p_in[i] + d_pq + q_out[i] - link[i]
            else:
                delta = pd_ + q_in[k] + q_out[k] - link[k]
            if cands is not None:
                cands.append((delta, i, k))
            elif best is None or delta < best[0]:
                best = (delta, i, k)
    if cands is not None:
        for delta, i, k in sorted(cands):
            new_seq = seq[:i] + [P] + seq[i:k] + [Q] + seq[k:]
            if ok(new_seq):
                return new_seq, delta
        return None
    if best is None:
        return None
    delta, i, k = best
    return seq[:i] + [P] + seq[i:k] + [Q] + seq[k:], delta


def find_best_combinations(
    orders: pd.DataFrame, cost: CostParams, opt: OptimizerParams, origin: Optional["Stop"] = None,
    waypoints: tuple = (),
    forced_ids: tuple = (),
    time_ctx: Optional[TimeCtx] = None
) -> tuple[list[Variant], dict]:
    """Zwraca (TOP warianty, statystyki). `orders` musi mieć kolumny współrzędnych (ltl.geo.add_coordinates).

    Dla każdego "głównego" (najdłuższego) zlecenia m dokładamy kolejne zlecenia z puli zgodnych (limit
    odchylenia względem m): na każdym poziomie rozwijamy `beam_width` najzyskowniejszych zestawów, więc
    wypełnienie naczepy rośnie tak długo, jak zysk albo zapełnienie na to pozwalają.
    `origin` = miejsce startu pojazdu: dojazd na pierwszy załadunek (puste km) wchodzi do zysku i kolejności punktów,
    a wariant dostaje na początku przystanek typu "S". `waypoints` = obowiązkowe punkty dodatkowe (Stop typu "W"):
    każdy trafia w najtańsze miejsce trasy (po starcie, przed ostatnim punktem zlecenia), a jego objazd liczy się do kosztów.
    `forced_ids` = numery zleceń, które muszą być w każdym wariancie (np. własny ładunek zbierany w punkcie dodatkowym);
    nie liczą się do limitu zleceń w wariancie, a limit odchylenia mierzony jest od trasy bazowej z nimi.
    `time_ctx` = okna czasowe ofert i zasady jazdy: kolejność punktów musi dać harmonogram mieszczący się w oknach
    (prędkość z ustawień, dzień pracy 9 h, odpoczynki); punkty dodatkowe trafiają w najtańsze miejsce, które się mieści.
    """
    stats = {"candidates": 0, "eligible": 0, "evaluated": 0, "truncated": False}
    forced_ids = set(forced_ids)
    max_evals = opt.max_evaluations * (2 if forced_ids else 1)       # własne ładunki zwiększają liczbę kombinacji
    is_forced = orders["id"].isin(forced_ids)
    df = orders[orders["price_eur"].notna() | is_forced].copy()
    df["price_eur"] = df["price_eur"].fillna(0.0)
    df = df[(df["ldm"] <= opt.max_ldm + 1e-9) & (df["weight_t"] <= opt.max_weight_t + 1e-9)]
    n_forced = int(df["id"].isin(forced_ids).sum())
    if n_forced < len(forced_ids) or len(df) - n_forced < 1 or (len(df) < 2 and not forced_ids):
        return [], stats                  # własny ładunek nie mieści się na naczepie albo brak zleceń z giełdy
    df = df.reset_index(drop=True)
    stats["eligible"] = len(df)

    n = len(df)
    lats = np.empty(2 * n)
    lons = np.empty(2 * n)
    lats[0::2], lons[0::2] = df["start_lat"].to_numpy(float), df["start_lon"].to_numpy(float)
    lats[1::2], lons[1::2] = df["end_lat"].to_numpy(float), df["end_lon"].to_numpy(float)
    D = road_distance_matrix(lats, lons)
    single = np.array([D[2 * i, 2 * i + 1] for i in range(n)])
    if n > opt.candidate_limit:  # zostaw zlecenia o najwyższej cenie za km
        score = df["price_eur"].to_numpy(float) / np.maximum(single, 50.0)
        score[df["id"].isin(forced_ids).to_numpy()] = np.inf       # własnych ładunków nie wolno odrzucić
        keep = np.sort(np.argsort(-score)[: opt.candidate_limit])
        df = df.iloc[keep].reset_index(drop=True)
        n = len(df)
        lats, lons = lats.reshape(-1, 2)[keep].ravel(), lons.reshape(-1, 2)[keep].ravel()
        D = road_distance_matrix(lats, lons)
        single = np.array([D[2 * i, 2 * i + 1] for i in range(n)])
    stats["candidates"] = n
    Dl = D.tolist()                       # odczyt pojedynczych komórek z listy list jest kilka razy szybszy niż z numpy

    ldm, wt = df["ldm"].to_numpy(float), df["weight_t"].to_numpy(float)
    price = df["price_eur"].to_numpy(float)
    forced = [i for i in range(n) if df["id"].iloc[i] in forced_ids]
    market = [i for i in range(n) if i not in set(forced)]
    d0 = None if origin is None else haversine_km(origin.lat, origin.lon, lats, lons) * ROAD_FACTOR
    time_on = time_ctx is not None and time_ctx.params.enabled
    pspec = None
    if time_on:
        hp = time_ctx.params
        ids = df["id"].tolist()
        pspec = [StopSpec("L" if p % 2 == 0 else "U", ids[p // 2],
                          (time_ctx.windows.get(ids[p // 2]) or {}).get("L" if p % 2 == 0 else "U"), hp.handling_h)
                 for p in range(2 * n)]
    plan = _Planner(D, lats, lons, d0, origin, tuple(waypoints), time_ctx if time_on else None, pspec)
    ok_seq = plan.seq_feasible if time_on else None
    wp_h = cost.waypoint_stop_h * len(waypoints)
    ldates = list(df["load_date"])

    # "główne" = najdłuższe; rozstrzyganie remisów indeksem
    rank = sorted(market, key=lambda i: (-single[i], i))
    pos = {i: r for r, i in enumerate(rank)}

    # ten sam ładunek ofertowany kilka razy (np. z różnymi stawkami) nie może wejść do wariantu dwa razy
    cargo = [(round(float(a), 3), round(float(b), 3), round(float(c), 3), round(float(d), 3), round(float(w), 2),
              round(float(m), 2), str(ld))
             for a, b, c, d, w, m, ld in zip(df["start_lat"], df["start_lon"], df["end_lat"], df["end_lon"],
                                              df["weight_t"], df["ldm"], df["load_date"])]

    gap = opt.max_load_date_gap_days
    if gap is None:
        date_ord = None
    elif all(x is None or type(x) is date for x in ldates):       # zwykłe daty: porównanie liczb zamiast obiektów pandas
        date_ord = [None if x is None else x.toordinal() for x in ldates]
    else:
        date_ord = False

    def pair_ok(a, b):
        if cargo[a] == cargo[b]:
            return False
        if gap is None:
            return True
        if date_ord is False:
            return _date_ok(ldates[a], ldates[b], gap)
        oa, ob = date_ord[a], date_ord[b]
        return oa is None or ob is None or abs(oa - ob) <= gap

    # trasa bazowa: zlecenie główne + zlecenia obowiązkowe (własne ładunki); bez obowiązkowych = samo zlecenie główne
    seed: dict[int, tuple] = {}
    for m in market:
        if not all(pair_ok(m, f) for f in forced):      # data odbioru własnego ładunku vs załadunek zlecenia głównego
            continue
        seq, ok = [2 * m, 2 * m + 1], True
        for f in sorted(forced, key=lambda f: D[2 * m, 2 * f]):
            ins = _insert(seq, f, Dl, ldm, wt, opt.max_ldm, opt.max_weight_t, ok_seq)
            if ins is None:
                ok = False
                break
            seq = ins[0]
        if not ok:
            continue
        if forced and len(forced) + 1 <= 3:
            r = _best_route(sorted([m, *forced]), Dl, ldm, wt, opt.max_ldm, opt.max_weight_t, ok=ok_seq)
            if r and r[0] < plan_len(D, seq):
                seq = r[1]
        if ok_seq is not None and not ok_seq(seq):         # zlecenie główne (z własnymi ładunkami) nie mieści się w oknach
            continue
        seed[m] = (seq, plan_len(D, seq))
    market = [m for m in market if m in seed]

    compat: dict[int, list[int]] = {}
    mk = np.array(market, dtype=int)
    for m in market:
        lst = []
        slack = seed[m][1] - single[m]             # o tyle trasa bazowa jest dłuższa przez własne ładunki
        limit = opt.max_detour_km + slack
        # Dolna granica objazdu dla zlecenia j (z nierówności trójkąta): każdy punkt X trasy przez A=załadunek m i
        # B=rozładunek m jest od niej oddalony co najmniej o min(|XA|, |XB|, |AX|+|XB|-|AB|). Jeśli któryś punkt
        # zlecenia j przekracza limit objazdu, żadna kolejność nie zmieści się w nim - nie ma po co jej szukać
        # (wynik jest ten sam, bo takie pary i tak odpadłyby w teście `r[0] - single[m] <= limit` poniżej).
        A, B = 2 * m, 2 * m + 1
        sm = D[A, B]
        too_far = np.zeros(len(mk), dtype=bool)
        for off in (0, 1):
            X = 2 * mk + off
            dA, dB = D[A, X], D[B, X]
            too_far |= np.minimum(np.minimum(dA, dB), dA + dB - sm) > limit + 1.0       # 1 km zapasu na błędy zaokrągleń
        far = dict(zip(market, too_far.tolist()))
        for j in market:
            if j == m or pos[j] < pos[m] or far[j] or not pair_ok(m, j):
                continue
            r = _best_route([m, j], Dl, ldm, wt, opt.max_ldm, opt.max_weight_t, ok=ok_seq)
            if r and r[0] - single[m] <= opt.max_detour_km + slack:       # wstępne odsiewanie; limit sprawdza wiązka
                lst.append((price[j] / (max(r[0] - single[m] - slack, 0.0) + 50.0), j))   # przychód za dodatkowy km
        lst.sort(reverse=True)
        compat[m] = [j for _, j in lst[: opt.compat_limit]]

    def score(idx, total, cal_h=0.0):
        c = profit(float(sum(price[i] for i in idx)), total, 2 * len(idx), cost, wp_h)
        pr = c["profit"]
        if time_on and time_ctx.params.charge_idle_days and cal_h > 0:      # dni kalendarzowe ponad dni pracy (czekanie)
            pr -= max(0.0, math.ceil(cal_h / 24.0 - 1e-9) - c["days"]) * cost.day_cost_eur
        return pr

    best: dict[frozenset, tuple] = {}     # zestaw -> (zysk, indeksy, kolejność punktów, dystans)
    evals = 0
    # zlecenia główne od najbardziej obiecujących (przychód za km); budżet obliczeń maleje z pozycją na liście,
    # więc przy dużych zbiorach najlepsze kandydatury dostają najgłębsze przeszukanie
    mains = sorted((m for m in market if compat[m] or forced), key=lambda m: -price[m] / max(single[m], 50.0))
    weights = [1.0 / (1.0 + k / 15.0) for k in range(len(mains))]
    scale = max_evals / max(sum(weights), 1e-9)
    for m, w in zip(mains, weights):
        if evals >= max_evals:
            stats["truncated"] = True
            break
        budget_end = evals + max(300, int(scale * w))
        cut = False
        key0 = frozenset([m, *forced])
        frontier = {key0: (list(seed[m][0]), float(seed[m][1]))}
        if forced:                                  # sam własny ładunek + zlecenie główne też jest wariantem
            f0 = plan.length(seed[m][0])
            pr0 = score(key0, f0)
            if key0 not in best or best[key0][0] < pr0:
                best[key0] = (pr0, tuple(sorted(key0)), list(seed[m][0]), float(seed[m][1]))
        for _level in range(2, opt.hard_cap_orders + 1):
            new: dict[frozenset, tuple] = {}
            for key, (seq, total) in frontier.items():
                for j in compat[m]:
                    if j in key or not all(pair_ok(j, x) for x in key):
                        continue
                    evals += 1
                    if evals > budget_end:        # budżet tego zlecenia głównego wyczerpany - idziemy do następnego
                        cut = stats["truncated"] = True
                        break
                    ins = _insert(seq, j, Dl, ldm, wt, opt.max_ldm, opt.max_weight_t, ok_seq)
                    if ins is None:
                        continue
                    nseq, delta = ins
                    ntotal = total + delta
                    if ntotal - seed[m][1] > opt.max_detour_km:
                        continue
                    fk = key | {j}
                    if fk in new and new[fk][1] <= ntotal:
                        continue
                    new[fk] = (nseq, ntotal)
                if cut:
                    break
            if not new:
                break
            scored = []
            infeasible = []
            for fk, (nseq, ntotal) in new.items():
                cal_h = 0.0
                if plan.trivial:
                    full = ntotal
                else:
                    full, cal_h = plan.length_and_hours(nseq)
                if len(fk) <= 3:  # małe zestawy: dokładna kolejność (z dojazdem ze startu)
                    r = _best_route(sorted(fk), Dl, ldm, wt, opt.max_ldm, opt.max_weight_t, d0=d0, ok=ok_seq)
                    if r:
                        alt, alt_h = (r[0], 0.0) if plan.trivial else plan.length_and_hours(r[1])
                        if alt < full - 1e-9:
                            nseq, full, cal_h = r[1], alt, alt_h
                            ntotal = plan.seq_length(nseq)
                            new[fk] = (nseq, ntotal)
                if full == float("inf"):               # nie mieści się w oknach czasowych (np. z punktem dodatkowym)
                    infeasible.append(fk)
                    continue
                scored.append((score(fk, full, cal_h), fk))
            for fk in infeasible:
                del new[fk]
            for pr, fk in scored:
                if fk not in best or best[fk][0] < pr:
                    best[fk] = (pr, tuple(sorted(fk)), new[fk][0], new[fk][1])
            scored.sort(key=lambda t: -t[0])
            frontier = {fk: new[fk] for _, fk in scored[: opt.beam_width]}
            if cut:
                break
    stats["evaluated"] = evals

    ranked = sorted(best.values(), key=lambda t: -t[0])[: opt.top_n]
    variants = [_build_variant(df, D, single, (pr, idx, total, seq), cost, ldm, wt, plan, wp_h)
                for pr, idx, seq, total in ranked]
    return variants, stats


class _Planner:
    """Pełna trasa: [start] + punkty zleceń + obowiązkowe punkty dodatkowe wstawione w najtańsze miejsce.

    Węzły: p >= 0 = punkt zlecenia (2i załadunek, 2i+1 rozładunek), -1 = start, -2-k = k-ty punkt dodatkowy."""

    def __init__(self, D, lats, lons, d0, origin, waypoints, tctx=None, pspec=None):
        self.D, self.d0, self.origin, self.wps = D, d0, origin, waypoints
        self.tctx, self.pspec = tctx, pspec
        self.trivial = origin is None and not waypoints and tctx is None
        self.dw = [haversine_km(w.lat, w.lon, lats, lons) * ROAD_FACTOR for w in waypoints]
        pts = ([(origin.lat, origin.lon)] if origin else []) + [(w.lat, w.lon) for w in waypoints]
        ids = ([-1] if origin else []) + [-2 - k for k in range(len(waypoints))]
        self.sp = {(a, b): float(haversine_km(*pa, *pb)) * ROAD_FACTOR
                   for a, pa in zip(ids, pts) for b, pb in zip(ids, pts)}

    def dist(self, a, b):
        if a >= 0 and b >= 0:
            return float(self.D[a, b])
        if a >= 0 or b >= 0:
            p, sp = (a, b) if a >= 0 else (b, a)
            return float(self.d0[p]) if sp == -1 else float(self.dw[-sp - 2][p])
        return self.sp[(a, b)]

    def seq_length(self, seq):
        return sum(float(self.D[a, b]) for a, b in zip(seq, seq[1:]))

    def feasible(self, nodes) -> bool:
        """Czy trasa [start] + punkty + punkty dodatkowe mieści się w oknach czasowych (przy zadanych zasadach jazdy)."""
        if self.tctx is None:
            return True
        hp = self.tctx.params
        specs = []
        for n in nodes:
            if n >= 0:
                specs.append(self.pspec[n])
            elif n == -1:
                specs.append(StopSpec("S", "START"))
            else:
                specs.append(StopSpec("W", self.wps[-n - 2].order_id, None, hp.waypoint_h))
        legs = [self.dist(a, b) / hp.speed_kmh for a, b in zip(nodes, nodes[1:])]
        return simulate(legs, specs, hp, fast=True).feasible

    def seq_feasible(self, seq) -> bool:
        return self.feasible(([-1] if self.origin else []) + list(seq))

    def nodes(self, seq):
        """Węzły trasy z wstawionymi punktami dodatkowymi (w najtańszym miejscu, które mieści się w terminach).
        None = nie da się wstawić punktu dodatkowego bez złamania okien czasowych."""
        nodes = ([-1] if self.origin else []) + list(seq)
        for k in range(len(self.wps)):
            w = -2 - k
            cands = []
            for i in range(1, len(nodes)):         # po starcie / pierwszym punkcie, przed ostatnim punktem zlecenia
                a, b = nodes[i - 1], nodes[i]
                cands.append((self.dist(a, w) + self.dist(w, b) - self.dist(a, b), i))
            cands.sort()
            for _, i in cands:
                trial = nodes[:i] + [w] + nodes[i:]
                if self.tctx is None or self.feasible(trial):
                    nodes = trial
                    break
            else:
                return None
        if self.tctx is not None and not self.wps and not self.feasible(nodes):
            return None
        return nodes

    def length(self, seq):
        return self.length_and_hours(seq)[0]

    def length_and_hours(self, seq):
        """(dystans, czas od wyjazdu do końca w godzinach); dystans = inf, gdy nie mieści się w oknach czasowych."""
        nodes = self.nodes(seq)
        if nodes is None:
            return float("inf"), 0.0
        dist = sum(self.dist(a, b) for a, b in zip(nodes, nodes[1:]))
        if self.tctx is None:
            return dist, 0.0
        hp = self.tctx.params
        specs = []
        for n in nodes:
            if n >= 0:
                specs.append(self.pspec[n])
            elif n == -1:
                specs.append(StopSpec("S", "START"))
            else:
                specs.append(StopSpec("W", self.wps[-n - 2].order_id, None, hp.waypoint_h))
        legs = [self.dist(a, b) / hp.speed_kmh for a, b in zip(nodes, nodes[1:])]
        return dist, simulate(legs, specs, hp, fast=True).total_h


def _build_variant(df, D, single, res, cost: CostParams, ldm=None, wt=None, plan=None, wp_h=0.0) -> Variant:
    pr, idx, total, seq = res
    nodes = (plan.nodes(seq) if plan else None) or list(seq)
    stops, loaded, empty = [], 0.0, 0.0
    on_board = set()
    for step, p in enumerate(nodes):
        if p == -1:
            stops.append(plan.origin)
        elif p <= -2:
            stops.append(plan.wps[-p - 2])
        else:
            i, is_unload = p // 2, bool(p % 2)
            row = df.iloc[i]
            side = "end" if is_unload else "start"
            stops.append(Stop(
                order_id=row["id"], kind="U" if is_unload else "L",
                country=row[f"{side}_country"], postal=row[f"{side}_postal"], city=row[f"{side}_city"],
                lat=float(row[f"{side}_lat"]), lon=float(row[f"{side}_lon"]),
            ))
        if step > 0:
            leg = plan.dist(nodes[step - 1], p) if plan else float(D[nodes[step - 1], p])
            if on_board:
                loaded += leg
            else:
                empty += leg
        if p >= 0:
            on_board.add(p // 2) if not p % 2 else on_board.discard(p // 2)
    total = loaded + empty
    peak_l = peak_w = cur_l = cur_w = 0.0
    for p in seq:
        sign = 1.0 if p % 2 == 0 else -1.0
        cur_l += sign * float(df.iloc[p // 2]["ldm"])
        cur_w += sign * float(df.iloc[p // 2]["weight_t"])
        peak_l, peak_w = max(peak_l, cur_l), max(peak_w, cur_w)
    revenue = float(sum(df.iloc[i]["price_eur"] for i in idx))
    main = max(single[i] for i in idx)
    c = profit(revenue, total, len(seq), cost, wp_h)
    if plan is not None and plan.tctx is not None and plan.tctx.params.charge_idle_days:
        cal_h = plan.length_and_hours(seq)[1]
        extra = max(0.0, math.ceil(cal_h / 24.0 - 1e-9) - c["days"]) if cal_h > 0 else 0.0
        c["profit"] -= extra * cost.day_cost_eur
        c["total"] += extra * cost.day_cost_eur
    return Variant(
        order_ids=[df.iloc[i]["id"] for i in idx], stops=stops, revenue=revenue, cost=c,
        profit=c["profit"], total_km=total, loaded_km=loaded, empty_km=empty,
        detour_km=float(plan.seq_length(seq) if plan else total) - main, days=c["days"], drive_h=c["drive_h"],
        peak_ldm=peak_l, peak_weight_t=peak_w,
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
