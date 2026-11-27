"""Harmonogram wariantu: okna czasowe ofert, limity jazdy i odpoczynki.

Czas liczymy w godzinach od 2000-01-01 (zwykłe liczby, żeby symulacja była szybka - optimizer wywołuje ją tysiące razy).

Model (uproszczony, ale zgodny z tym, co liczą koszty):
* dzień pracy to maks. `max_work_h_day` godzin jazdy i obsługi (załadunek/rozładunek), potem odpoczynek dobowy,
* w tygodniu maks. `max_drive_h_week` godzin jazdy, potem odpoczynek tygodniowy,
* przyjazd przed otwarciem okna = czekanie (jeśli trwa co najmniej tyle, co odpoczynek dobowy, liczy się jako odpoczynek),
* przyjazd po zamknięciu okna (z tolerancją) = wariant nie mieści się w terminach,
* oferta bez godzin (sama data) ma okno od `open_h` do `close_h` tego dnia,
* start: podana data i godzina albo „automatycznie” - pojazd rusza dokładnie tak, by zdążyć na początek pierwszego okna.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd

EPOCH = datetime(2000, 1, 1)


def to_h(dt) -> float:
    return (pd.Timestamp(dt).to_pydatetime().replace(tzinfo=None) - EPOCH).total_seconds() / 3600.0


def from_h(h: float) -> datetime:
    return EPOCH + timedelta(hours=h)


@dataclass(frozen=True)
class ScheduleParams:
    enabled: bool = True              # odrzucaj warianty niemieszczące się w oknach
    speed_kmh: float = 65.0
    max_work_h_day: float = 9.0
    max_drive_h_week: float = 56.0
    daily_rest_h: float = 11.0
    weekly_rest_h: float = 48.0
    handling_h: float = 2.0
    waypoint_h: float = 0.0
    open_h: float = 6.0               # godziny przeładunków, gdy oferta podaje tylko datę
    close_h: float = 18.0
    tolerance_h: float = 0.0          # o tyle godzin wolno się spóźnić na koniec okna
    charge_idle_days: bool = True     # doliczaj do kosztów dni kalendarzowe ponad dni pracy (czekanie na okna, odpoczynki)
    start_at: Optional[datetime] = None   # None = automatycznie


@dataclass(frozen=True)
class StopSpec:
    kind: str                          # S start, W punkt dodatkowy, L załadunek, U rozładunek
    order_id: str
    window: Optional[tuple] = None     # (od_h, do_h) albo None
    service_h: float = 0.0


@dataclass
class StopTiming:
    kind: str
    order_id: str
    arrive_h: float
    start_h: float
    end_h: float
    wait_h: float
    rest_h: float                      # odpoczynki wpadające w drodze do tego punktu
    window: Optional[tuple]
    late_h: float


@dataclass
class ScheduleResult:
    stops: list = field(default_factory=list)
    feasible: bool = True
    max_late_h: float = 0.0
    start_h: float = 0.0
    end_h: float = 0.0
    wait_h: float = 0.0
    rest_h: float = 0.0

    @property
    def total_h(self) -> float:
        return self.end_h - self.start_h


# ----------------------------------------------------------------- okna z ofert
def _is_na(x) -> bool:
    return x is None or (isinstance(x, float) and x != x) or (x is pd.NaT) or (not isinstance(x, (str, bytes)) and pd.isna(x))


def resolve_window(w_from, w_to, day, p: ScheduleParams) -> Optional[tuple]:
    """Okno (od_h, do_h) z kolumn from/to oraz daty zapasowej. Pola z godziną 00:00:00 (od) i 23:59:59 (do) oznaczają
    „tylko data”: wtedy obowiązują godziny przeładunków z ustawień."""
    open_td, close_td = timedelta(hours=p.open_h), timedelta(hours=p.close_h)
    f = None if _is_na(w_from) else pd.Timestamp(w_from).to_pydatetime()
    t = None if _is_na(w_to) else pd.Timestamp(w_to).to_pydatetime()
    if f is None and not _is_na(day):
        d0 = datetime.combine(pd.Timestamp(day).date(), datetime.min.time())
        return to_h(d0 + open_td), to_h(d0 + close_td)
    if f is None:
        return None
    d0 = datetime.combine(f.date(), datetime.min.time())
    fe = d0 + open_td if f == d0 else f
    if t is None:
        te = d0 + close_td
    else:
        d1 = datetime.combine(t.date(), datetime.min.time())
        te = d1 + close_td if (t.hour, t.minute, t.second) == (23, 59, 59) else t
    te = max(te, fe)
    return to_h(fe), to_h(te)


def order_windows(df: pd.DataFrame, p: ScheduleParams) -> dict:
    """{numer zlecenia: {"L": okno | None, "U": okno | None}}."""
    out = {}
    cols = df.columns
    for r in df.to_dict("records"):
        out[r["id"]] = {
            "L": resolve_window(r.get("load_from") if "load_from" in cols else None,
                                r.get("load_to") if "load_to" in cols else None, r.get("load_date"), p),
            "U": resolve_window(r.get("unload_from") if "unload_from" in cols else None,
                                r.get("unload_to") if "unload_to" in cols else None, r.get("unload_date"), p),
        }
    return out


def sync_windows(df: pd.DataFrame) -> pd.DataFrame:
    """Po ręcznej zmianie daty w tabeli stare okno (z godzinami) przestaje pasować: wracamy do okna z samej daty.
    (Operacje na całych kolumnach; wcześniejsza pętla po wierszach była wolna przy kilkuset zleceniach.)"""
    df = df.copy()
    for side in ("load", "unload"):
        fcol, tcol, dcol = f"{side}_from", f"{side}_to", f"{side}_date"
        if fcol not in df.columns:
            continue
        f = pd.to_datetime(df[fcol], errors="coerce")
        has_f = f.notna()
        if not has_f.any():
            continue
        d = pd.to_datetime(df[dcol], errors="coerce")
        d_na = d.isna()
        fill = has_f & d_na                                   # brak daty, jest okno: data z okna
        clear = has_f & ~d_na & (f.dt.normalize() != d.dt.normalize())   # data zmieniona ręcznie: okno nieaktualne
        if fill.any():
            col = df[dcol].astype(object)
            col[fill] = f[fill].dt.date
            df[dcol] = col
        if clear.any():
            df.loc[clear, fcol] = pd.NaT
            df.loc[clear, tcol] = pd.NaT
    return df


# ----------------------------------------------------------------- symulacja
def simulate(leg_hours: list, specs: list, p: ScheduleParams, fast: bool = False) -> ScheduleResult:
    """Harmonogram dla ciągu przystanków `specs` i odcinków `leg_hours` (godziny jazdy, len = len(specs) - 1).

    fast=True: zatrzymuje się na pierwszym naruszeniu okna i nie buduje listy przystanków (do pętli optymalizatora)."""
    res = ScheduleResult()
    t = 0.0
    work = 0.0                 # godziny pracy od ostatniego odpoczynku dobowego
    drive_w = 0.0              # godziny jazdy od ostatniego odpoczynku tygodniowego
    shifted = p.start_at is not None
    if shifted:
        t = to_h(p.start_at)
    first_window_seen = False
    rest_total = wait_total = 0.0
    max_late = 0.0
    start_h = t
    tol = p.tolerance_h
    for i, sp in enumerate(specs):
        rest_here = 0.0
        if i > 0:
            h = leg_hours[i - 1]
            while h > 1e-9:
                if drive_w >= p.max_drive_h_week - 1e-9:
                    t += p.weekly_rest_h; rest_here += p.weekly_rest_h; drive_w = 0.0; work = 0.0
                    continue
                can = min(h, p.max_work_h_day - work, p.max_drive_h_week - drive_w)
                if can <= 1e-9:
                    t += p.daily_rest_h; rest_here += p.daily_rest_h; work = 0.0
                    continue
                t += can; work += can; drive_w += can; h -= can
        arrive = t
        wait = 0.0
        late = 0.0
        w = sp.window
        if w is not None:
            if not shifted and not first_window_seen:
                # start automatyczny: cofamy cały dotychczasowy przebieg tak, żeby przyjazd = początek pierwszego okna
                shift = w[0] - t
                t += shift; arrive += shift; start_h += shift
                first_window_seen = True
            if t < w[0]:
                wait = w[0] - t
                if wait >= p.weekly_rest_h:
                    drive_w = 0.0
                if wait >= p.daily_rest_h:
                    work = 0.0
                t = w[0]
            late = max(0.0, t - w[1])
            if late > tol + 1e-9:
                res.feasible = False
                if fast:
                    res.max_late_h = late
                    return res
            max_late = max(max_late, late)
        srv = sp.service_h
        start_service = t
        while srv > 1e-9:
            can = min(srv, p.max_work_h_day - work)
            if can <= 1e-9:
                t += p.daily_rest_h; rest_here += p.daily_rest_h; work = 0.0
                continue
            t += can; work += can; srv -= can
        rest_total += rest_here
        wait_total += wait
        if not fast:
            res.stops.append(StopTiming(sp.kind, sp.order_id, arrive, start_service, t, wait, rest_here, w, late))
    res.max_late_h = max_late
    res.start_h, res.end_h, res.wait_h, res.rest_h = start_h, t, wait_total, rest_total
    return res


def specs_for_stops(stops, windows: dict, p: ScheduleParams) -> list:
    """StopSpec dla przystanków wariantu (Stop z ltl.optimizer) i okien z order_windows."""
    out = []
    for s in stops:
        if s.kind == "S":
            out.append(StopSpec("S", s.order_id))
        elif s.kind == "W":
            out.append(StopSpec("W", s.order_id, None, p.waypoint_h))
        else:
            out.append(StopSpec(s.kind, s.order_id, (windows.get(s.order_id) or {}).get(s.kind), p.handling_h))
    return out
