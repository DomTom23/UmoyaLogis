"""Opis wariantu po wyborze: kolejność przystanków z zapełnieniem naczepy oraz lista zleceń do odnalezienia na giełdzie."""
from __future__ import annotations

import pandas as pd

from .optimizer import Variant
from .routing import RouteResult
from .schedule import ScheduleResult, from_h

LOAD_ICON, UNLOAD_ICON, START_ICON, WAYPOINT_ICON = "🟢▲", "🔴▼", "🏁", "📍"
LOAD_COLOR, UNLOAD_COLOR = "#1a9850", "#d73027"


def arrow(kind: str) -> str:
    """Strzałka do tabel (emoji, bo tabele nie kolorują fragmentów tekstu): zielona = załadunek, czerwona = rozładunek."""
    return {"S": START_ICON, "W": WAYPOINT_ICON, "L": LOAD_ICON}.get(kind, UNLOAD_ICON)


def arrow_html(kind: str) -> str:
    if kind in ("S", "W"):
        return f"<span style='font-weight:700'>{arrow(kind)}</span>"
    color, sign = (LOAD_COLOR, "▲") if kind == "L" else (UNLOAD_COLOR, "▼")
    return f"<span style='color:{color};font-weight:700'>{sign}</span>"


def route_chain_html(variant: Variant) -> str:
    """Przebieg wariantu w jednym wierszu: kolorowe strzałki, numer zlecenia, kraj i kod."""
    parts = [f"{arrow_html(s.kind)}&nbsp;<b>{'Start' if s.kind == 'S' else s.order_id}</b>&nbsp;{s.country}&nbsp;{s.postal}"
             for s in variant.stops]
    return " → ".join(parts)


def _fmt_date(x) -> str:
    return "" if x is None or pd.isna(x) else pd.Timestamp(x).strftime("%d.%m")


def place(country, postal, city) -> str:
    return f"{country}-{postal} {city}".strip()


def window_text(w) -> str:
    if not w:
        return "—"
    a, b = from_h(w[0]), from_h(w[1])
    return f"{a:%d.%m %H:%M} – {b:%H:%M}" if a.date() == b.date() else f"{a:%d.%m %H:%M} – {b:%d.%m %H:%M}"


def stop_table(variant: Variant, route: RouteResult, orders: pd.DataFrame,
               schedule: ScheduleResult | None = None) -> pd.DataFrame:
    """Krok po kroku: co robimy, gdzie, ile jest na naczepie po operacji i ile km/h do następnego przystanku."""
    by_id = orders.set_index("id")
    ldm = wt = 0.0
    cum_km = cum_h = 0.0
    rows = []
    for i, s in enumerate(variant.stops):
        leg = route.legs[i] if i < len(route.legs) else None
        if s.kind in ("S", "W"):
            rows.append({"Krok": i + 1, "Operacja": f"{arrow(s.kind)} {'Start pojazdu' if s.kind == 'S' else 'Punkt dodatkowy'}",
                         "Zlecenie": "—" if s.kind == "S" else s.order_id, "Zleceniodawca": "—",
                         "Miejsce": place(s.country, s.postal, s.city), "Data": "—", "Ładunek": "—",
                         "Na naczepie po": f"{ldm:.1f} LDM · {wt:.1f} t",
                         "Do następnego": f"{leg.km:,.0f} km · {leg.hours:.1f} h ".replace(",", " ") if leg else "—",
                         "Od startu (km)": round(cum_km)})
            if leg:
                cum_km += leg.km
                cum_h += leg.hours
            continue
        o = by_id.loc[s.order_id]
        sign = 1 if s.kind == "L" else -1
        ldm = max(ldm + sign * float(o["ldm"]), 0.0)
        wt = max(wt + sign * float(o["weight_t"]), 0.0)
        rows.append({
            "Krok": i + 1,
            "Operacja": f"{arrow(s.kind)} {'Załadunek' if s.kind == 'L' else 'Rozładunek'}",
            "Zlecenie": s.order_id,
            "Zleceniodawca": o.get("client") or "—",
            "Miejsce": place(s.country, s.postal, s.city),
            "Data": _fmt_date(o["load_date"] if s.kind == "L" else o["unload_date"]) or "—",
            "Ładunek": f"{float(o['ldm']):.1f} LDM · {float(o['weight_t']):.1f} t",
            "Na naczepie po": f"{ldm:.1f} LDM · {wt:.1f} t",
            "Do następnego": f"{leg.km:,.0f} km · {leg.hours:.1f} h".replace(",", " ") if leg else "—",
            "Od startu (km)": round(cum_km),
        })
        if leg:
            cum_km += leg.km
            cum_h += leg.hours
    df = pd.DataFrame(rows)
    if schedule is not None and len(schedule.stops) == len(df):
        st = schedule.stops
        df.insert(4, "Przyjazd", [f"{from_h(x.arrive_h):%d.%m %H:%M}" for x in st])
        df.insert(5, "Okno", [window_text(x.window) for x in st])
        def status(x):
            parts = [("⚠ spóźnienie " + f"{x.late_h:.1f} h") if x.late_h > 1e-9 else ("czeka " + f"{x.wait_h:.1f} h" if x.wait_h >= 0.05 else "✓")]
            if x.rest_h:
                parts.append(f"odpoczynek {x.rest_h:.0f} h w drodze")
            return " · ".join(parts)
        df.insert(6, "Status", [status(x) for x in st])
        df = df.drop(columns=["Data", "Ładunek", "Od startu (km)"])         # z harmonogramem tabela musi się zmieścić na ekranie
    return df


def order_table(variant: Variant, orders: pd.DataFrame, estimated: set | None = None) -> pd.DataFrame:
    """Zlecenia wariantu z kolumną „Kody do wyszukania” (kraj + kod załadunku i rozładunku, bo nazwa zleceniodawcy
    często nie jest widoczna w kopiowanych ofertach Trans.eu); LDM, waga i cena pomagają wybrać właściwą ofertę."""
    estimated = estimated or set()
    by_id = orders.set_index("id")
    rows = []
    for oid in variant.order_ids:
        o = by_id.loc[oid]
        a, b = place(o["start_country"], o["start_postal"], o["start_city"]), \
            place(o["end_country"], o["end_postal"], o["end_city"])
        dist = o["distance_km"] if pd.notna(o.get("distance_km")) else o.get("distance_est_km")
        price = o["price_eur"]
        rows.append({
            "Zlecenie": oid,
            "Zleceniodawca": o.get("client") or "—",
            "Kody do wyszukania": f"{o['start_country']} {o['start_postal']} → {o['end_country']} {o['end_postal']}",
            "🟢▲ Załadunek": f"{a} · {_fmt_date(o['load_date']) or '—'}",
            "🔴▼ Rozładunek": f"{b} · {_fmt_date(o['unload_date']) or '—'}",
            "LDM": float(o["ldm"]),
            "Waga (t)": float(o["weight_t"]),
            "Cena (EUR)": float(price),
            "EUR/km": round(float(price) / float(dist), 2) if pd.notna(dist) and dist else None,
            "Cena z": "szacunek" if oid in estimated else "oferty",
        })
    return pd.DataFrame(rows)
