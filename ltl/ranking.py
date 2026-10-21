"""Ranking wariantów po trasach rzeczywistych (po drogach dla ciężarówek)."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable, Optional

import pandas as pd

from .costs import CostParams
from .optimizer import Variant
from .route_edit import evaluate_route
from .routing import RouteResult


def variant_key(v: Variant) -> str:
    return "|".join(f"{s.order_id}{s.kind}" for s in v.stops)


@dataclass
class Ranked:
    variant: Variant
    route: RouteResult
    ev: dict

    @property
    def key(self) -> str:
        return variant_key(self.variant)


def rank_by_real_routes(variants: list[Variant], cost: CostParams, route_fn: Callable[[Variant], RouteResult],
                        top_n: int, workers: int = 4,
                        progress: Optional[Callable[[int, int], None]] = None) -> Optional[list[Ranked]]:
    """Wyznacza trasy po drogach dla wszystkich kandydatów i sortuje po rzeczywistym zysku.

    Zwraca None, gdy routing jest niedostępny (pierwszy wariant bez żadnego odcinka z silnika tras) -
    wtedy nie męczymy serwera kolejnymi zapytaniami.
    """
    if not variants:
        return []
    first = route_fn(variants[0])
    if not first.has_real:
        return None
    done = [Ranked(variants[0], first, evaluate_route(variants[0], first, cost))]
    if progress:
        progress(1, len(variants))
    if len(variants) > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(route_fn, v): v for v in variants[1:]}
            for n, fut in enumerate(as_completed(futures), start=2):
                v = futures[fut]
                r = fut.result()
                done.append(Ranked(v, r, evaluate_route(v, r, cost)))
                if progress:
                    progress(n, len(variants))
    done.sort(key=lambda x: -x.ev["profit"])
    return done[:top_n]


def crossing_text(crossings) -> str:
    return "; ".join(c.label for c in crossings) if crossings else "—"


def ranked_to_frame(ranked: list[Ranked]) -> pd.DataFrame:
    rows = []
    for rank, r in enumerate(ranked, start=1):
        v, ev = r.variant, r.ev
        route = " → ".join(f"{'▲' if s.kind == 'L' else '▼'} {s.order_id} {s.country} {s.postal}" for s in v.stops)
        rows.append({
            "#": rank,
            "Zlecenia": " + ".join(v.order_ids),
            "Liczba": len(v.order_ids),
            "Trasa": route,
            "Zapełnienie (LDM)": round(v.peak_ldm, 1),
            "Ładunek (t)": round(v.peak_weight_t, 1),
            "Przychód (EUR)": round(ev["revenue"], 0),
            "Koszty (EUR)": round(ev["total"], 0),
            "Zysk (EUR)": round(ev["profit"], 0),
            "Zysk/dzień (EUR)": round(ev["profit"] / max(ev["days"], 1), 0),
            "Czas (dni)": round(ev["days"], 1),
            "Dystans (km)": round(ev["total_km"], 0),
            "Km puste": round(ev["empty_km"], 0),
            "Przeprawy i tunele": crossing_text(ev["crossings"]),
        })
    return pd.DataFrame(rows)
