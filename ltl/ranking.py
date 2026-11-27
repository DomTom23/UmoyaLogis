"""Ranking wariantów po trasach rzeczywistych (po drogach dla ciężarówek)."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable, Optional

import pandas as pd

from .costs import CostParams
from .optimizer import TimeCtx, Variant
from .route_edit import evaluate_route
from .routing import RouteResult
from .schedule import from_h
from .variant_detail import arrow


def variant_key(v: Variant) -> str:
    # start pojazdu nie ma numeru zlecenia, więc klucz zawiera jego położenie (inny start = inna trasa)
    return "|".join(f"{s.order_id}@{s.lat:.4f},{s.lon:.4f}{s.kind}" if s.kind in ("S", "W") else f"{s.order_id}{s.kind}"
                    for s in v.stops)


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
                        progress: Optional[Callable[[int, int], None]] = None,
                        sched: Optional[TimeCtx] = None) -> Optional[list[Ranked]]:
    """Wyznacza trasy po drogach dla wszystkich kandydatów i sortuje po rzeczywistym zysku.

    Zwraca None, gdy routing jest niedostępny (pierwszy wariant bez żadnego odcinka z silnika tras) -
    wtedy nie męczymy serwera kolejnymi zapytaniami.
    """
    if not variants:
        return []
    first = route_fn(variants[0])
    if not first.has_real:
        return None
    done = [Ranked(variants[0], first, evaluate_route(variants[0], first, cost, sched))]
    if progress:
        progress(1, len(variants))
    if len(variants) > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(route_fn, v): v for v in variants[1:]}
            for n, fut in enumerate(as_completed(futures), start=2):
                v = futures[fut]
                r = fut.result()
                done.append(Ranked(v, r, evaluate_route(v, r, cost, sched)))
                if progress:
                    progress(n, len(variants))
    done.sort(key=lambda x: -x.ev["profit"])
    if sched is not None and sched.params.enabled:
        # warianty, które po prawdziwych trasach nie mieszczą się w oknach czasowych, odpadają; gdy odpadną wszystkie,
        # zostawiamy najlepsze (z ostrzeżeniem w tabeli), żeby użytkownik zobaczył, co poszło nie tak
        feasible = [x for x in done if x.ev["schedule"].feasible]
        if feasible:
            done = feasible
    return done[:top_n]


def schedule_text(sch) -> str:
    if sch is None:
        return "—"
    if not sch.feasible:
        return f"⚠ spóźnienie {sch.max_late_h:.1f} h"
    return "✓ w terminach"


def crossing_text(crossings) -> str:
    return "; ".join(c.label for c in crossings) if crossings else "—"


def ranked_to_frame(ranked: list[Ranked], clients: dict | None = None, estimated: set | None = None) -> pd.DataFrame:
    """Tabela wariantów. clients: nr zlecenia -> zleceniodawca; estimated: numery zleceń z ceną szacunkową."""
    clients, estimated = clients or {}, estimated or set()
    rows = []
    for rank, r in enumerate(ranked, start=1):
        v, ev = r.variant, r.ev
        route = " → ".join(f"{arrow(s.kind)} {'Start' if s.kind == 'S' else s.order_id} {s.country} {s.postal}" for s in v.stops)
        rows.append({
            "#": rank,
            "Zlecenia": " + ".join(v.order_ids),
            "Liczba": len(v.order_ids),
            "Zleceniodawcy": ", ".join(dict.fromkeys(c for i in v.order_ids if (c := clients.get(i)))) or "—",
            "Ceny szac.": ", ".join(i for i in v.order_ids if i in estimated) or "—",
            "Zysk (EUR)": round(ev["profit"], 0),
            "Zysk/dzień (EUR)": round(ev["profit"] / max(ev["days"], 1), 0),
            "Przychód (EUR)": round(ev["revenue"], 0),
            "Koszty (EUR)": round(ev["total"], 0),
            "Dystans (km)": round(ev["total_km"], 0),
            "Cena/km (EUR)": round(ev["revenue"] / ev["total_km"], 2) if ev["total_km"] else 0.0,
            "Czas (dni)": round(ev["days"], 1),
            "Harmonogram": schedule_text(ev.get("schedule")),
            "Zakończenie": from_h(ev["schedule"].end_h).strftime("%d.%m %H:%M") if ev.get("schedule") else "—",
            "Km puste": round(ev["empty_km"], 0),
            "Zapełnienie (LDM)": round(v.peak_ldm, 1),
            "Ładunek (t)": round(v.peak_weight_t, 1),
            "Trasa": route,
            "Przeprawy i tunele": crossing_text(ev["crossings"]),
        })
    return pd.DataFrame(rows)
