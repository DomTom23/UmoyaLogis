"""Tabela tras i zleceń w układzie arkusza firmy: wiersz trasy (sumy i kalkulacje całej trasy) + wiersze jej zleceń.

Numeracja: trasa ``TR-RRRR/MM-NNNN``, jej zlecenia ``TR-RRRR/MM-NNNN-01``, ``-02``… (kolejność jak w planie).
Wiersz trasy: Skąd = miejsce rozładunku poprzedniej trasy pojazdu (dla pierwszej: miejsce startu pojazdu), Dokąd = rozładunek
ostatniego zlecenia, Start = data rozładunku poprzedniej trasy, Koniec = data rozładunku ostatniego zlecenia, reszta to sumy
i kalkulacje całej trasy. Wiersze zleceń biorą dane ze zleceń; koszty trasy rozkładamy na zlecenia proporcjonalnie do ich km
(suma marż zleceń = marża trasy).
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from typing import Callable

import pandas as pd

from .geo import ROAD_FACTOR, haversine_km
from .workspace import PAY_STATUSES, PAY_TERMS

COLUMNS = ["Nr trasy/zlecenia", "Skąd", "Dokąd", "Dni trasy", "Start/Załadunek", "Koniec/Rozładunek", "Zleceniodawca",
           "Frachty (€)", "Dodatkowe wynagrodzenie (€)", "Łącznie (€)", "Dyspozytor", "Płatność status", "Termin płatności",
           "Płatność do", "Km", "Stawka €/km", "LDM", "€/LDM", "Waga (t)", "Marża €/km", "Marża łącznie (€)",
           "Koszt razem (€)", "Koszt dzienny (€)", "Paliwo (€)", "Opłaty drogowe (€)", "Prom/tunel (€)"]
PAID_KINDS = ("Prom", "Pociąg", "Tunel", "Inny")            # rodzaje płatnych odcinków trasy (lista rozwijana)
TERM_LABELS = [f"{d} dni" for d in PAY_TERMS]


def term_label(days) -> str | None:
    return None if days is None or (isinstance(days, float) and math.isnan(days)) else f"{int(days)} dni"


def term_days(label) -> int | None:
    """„30 dni” -> 30 (None dla pustego/nieznanego)."""
    try:
        d = int(str(label).split()[0])
    except (ValueError, IndexError):
        return None
    return d if d in PAY_TERMS else None


def order_code(route_code: str, index: int) -> str:
    return f"{route_code}-{index + 1:02d}"


def _d(iso) -> date | None:
    try:
        return date.fromisoformat(iso) if iso else None
    except ValueError:
        return None


def fmt_date(d: date | None) -> str | None:
    return d.strftime("%d.%m.%Y") if d else None


def place_label(country: str, postal: str, city: str, namer: Callable[[str, str], str] | None = None) -> str:
    """„DE · Berlin”: kraj + miejscowość (z oferty albo odczytana z kodu pocztowego); bez nazwy - sam kod pocztowy."""
    country, postal, city = (country or "").strip().upper(), (postal or "").strip(), (city or "").strip()
    name = city or (namer(country, postal) if namer and country and postal else "") or postal
    return f"{country} · {name}" if country and name else (country or name or "—")


def paid_total(plan: dict) -> float:
    return float(sum(float(s.get("cost_eur") or 0) for s in plan.get("paid_sections", [])))


def _ratio(a, b):
    return round(a / b, 2) if a is not None and b else None


def _order_km(o: dict) -> float | None:
    """Km zlecenia: z oferty, a gdy jej brak - szacunek z współrzędnych (linia prosta × współczynnik drogowy)."""
    if o.get("distance_km"):
        return o["distance_km"]
    pts = [o.get("start_lat"), o.get("start_lon"), o.get("end_lat"), o.get("end_lon")]
    return float(haversine_km(*pts)) * ROAD_FACTOR if None not in pts else None


def end_of(plan: dict, namer: Callable[[str, str], str] | None = None) -> dict:
    """Koniec trasy: miejsce i data ostatniego rozładunku (wg kolejności punktów, a bez niej - ostatniego zlecenia)."""
    orders = plan["orders"]
    last = None
    for s in reversed(plan.get("stops", [])):
        if s["kind"] == "U":
            last = next((x for x in orders if x["id"] == s["order_id"]), None)
            if last:
                break
    last = last or (orders[-1] if orders else None)
    if not last:
        return {"place": "—", "date": None}
    return {"place": place_label(last["end_country"], last["end_postal"], last["end_city"], namer),
            "date": _d(last["unload_date"])}


def warm_places(plans: list[dict], namer: Callable[[str, str], str], workers: int = 4) -> None:
    """Odczytuje z kodów pocztowych nazwy miejscowości wszystkich zleceń naraz (bez miejscowości podanej w ofercie)."""
    from concurrent.futures import ThreadPoolExecutor
    pairs = {(o[f"{side}_country"], o[f"{side}_postal"]) for pl in plans for o in pl["orders"] for side in ("start", "end")
             if o[f"{side}_country"] and o[f"{side}_postal"] and not o[f"{side}_city"]}
    if pairs:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(lambda cp: namer(*cp), pairs))


def build_sheet(code: str, plan: dict, ev: dict | None, km: float | None, calendar_days: float | None,
                prev_end: dict | None, namer: Callable[[str, str], str] | None = None) -> tuple[dict, list[dict], dict]:
    """(wiersz trasy, wiersze zleceń, {"place", "date"} końca trasy - do łączenia z następną trasą).

    ``ev`` - wynik ``evaluate_route`` (None, gdy trasy nie dało się policzyć), ``km`` - długość całej trasy,
    ``calendar_days`` - dni trasy z harmonogramu, ``prev_end`` - koniec poprzedniej trasy pojazdu (lub miejsce startu)."""
    orders = plan["orders"]
    revenue = sum(o["price_eur"] for o in orders)
    extras = sum(o.get("extra_pay_eur") or 0.0 for o in orders)
    total_rev = revenue + extras
    ldm = sum(o["ldm"] for o in orders)
    weight = sum(o["weight_t"] for o in orders)
    cost_total = ev["total"] if ev else None            # koszty i zysk z ``ev`` zawierają już płatne odcinki z planu
    profit = ev["profit"] if ev else None
    fees = ev["fees"] if ev else (paid_total(plan) or None)

    # rozdział kosztów na zlecenia: proporcjonalnie do km zleceń (gdy brak km - po równo)
    kms = [_order_km(o) for o in orders]
    km_sum = sum(k for k in kms if k) or 0.0
    shares = [(k / km_sum) if (km_sum and k) else (0.0 if km_sum else 1 / max(len(orders), 1)) for k in kms]

    order_rows = []
    for i, (o, k, sh) in enumerate(zip(orders, kms, shares)):
        line = o["price_eur"] + (o.get("extra_pay_eur") or 0.0)
        margin = line - cost_total * sh if cost_total is not None else None
        ld, ud = _d(o.get("load_date")), _d(o.get("unload_date"))
        due = fmt_date(ud + timedelta(days=o["pay_term_days"])) if ud and o.get("pay_term_days") else None
        order_rows.append({
            "Nr trasy/zlecenia": order_code(code, i),
            "Skąd": place_label(o["start_country"], o["start_postal"], o["start_city"], namer),
            "Dokąd": place_label(o["end_country"], o["end_postal"], o["end_city"], namer),
            "Dni trasy": (ud - ld).days + 1 if ld and ud and ud >= ld else None,
            "Start/Załadunek": fmt_date(ld), "Koniec/Rozładunek": fmt_date(ud),
            "Zleceniodawca": o["client"] or None,
            "Frachty (€)": o["price_eur"], "Dodatkowe wynagrodzenie (€)": o.get("extra_pay_eur") or 0.0, "Łącznie (€)": line,
            "Dyspozytor": o.get("dispatcher") or None, "Płatność status": o.get("pay_status") or PAY_STATUSES[0],
            "Termin płatności": term_label(o.get("pay_term_days")), "Płatność do": due,
            "Km": round(k) if k else None, "Stawka €/km": _ratio(line, k), "LDM": o["ldm"] or None,
            "€/LDM": _ratio(line, o["ldm"]), "Waga (t)": o["weight_t"] or None,
            "Marża €/km": _ratio(margin, k), "Marża łącznie (€)": round(margin, 2) if margin is not None else None,
            "Koszt razem (€)": None, "Koszt dzienny (€)": None, "Paliwo (€)": None, "Opłaty drogowe (€)": None,
            "Prom/tunel (€)": None})

    end = end_of(plan, namer)
    end_place, end_date = end["place"], end["date"]
    days = calendar_days if calendar_days else (
        sum(r["Dni trasy"] or 0 for r in order_rows) or None)
    route_row = {
        "Nr trasy/zlecenia": code,
        "Skąd": (prev_end or {}).get("place") or "—", "Dokąd": end_place,
        "Dni trasy": int(math.ceil(days - 1e-9)) if days else None,
        "Start/Załadunek": fmt_date((prev_end or {}).get("date")), "Koniec/Rozładunek": fmt_date(end_date),
        "Zleceniodawca": None,
        "Frachty (€)": revenue, "Dodatkowe wynagrodzenie (€)": extras, "Łącznie (€)": total_rev,
        "Dyspozytor": None, "Płatność status": None, "Termin płatności": None, "Płatność do": None,
        "Km": round(km) if km else None, "Stawka €/km": _ratio(total_rev, km), "LDM": ldm or None,
        "€/LDM": _ratio(total_rev, ldm), "Waga (t)": weight or None,
        "Marża €/km": _ratio(profit, km), "Marża łącznie (€)": round(profit, 2) if profit is not None else None,
        "Koszt razem (€)": round(cost_total, 2) if cost_total is not None else None,
        "Koszt dzienny (€)": round(cost_total / math.ceil(days - 1e-9), 2) if cost_total is not None and days else None,
        "Paliwo (€)": round(ev["fuel"], 2) if ev else None, "Opłaty drogowe (€)": round(ev["tolls"], 2) if ev else None,
        "Prom/tunel (€)": round(fees, 2) if fees is not None else None}
    return route_row, order_rows, end


TEXT_COLUMNS = ("Nr trasy/zlecenia", "Skąd", "Dokąd", "Start/Załadunek", "Koniec/Rozładunek", "Zleceniodawca", "Dyspozytor",
                "Płatność status", "Termin płatności", "Płatność do")


def sheet_frame(rows: list[dict]) -> pd.DataFrame:
    """Tabela w układzie arkusza: kolumny tekstowe (puste = ""), liczbowe jako liczby (puste = brak wartości)."""
    df = pd.DataFrame(rows, columns=COLUMNS)
    for c in COLUMNS:
        df[c] = df[c].fillna("").astype(str) if c in TEXT_COLUMNS else pd.to_numeric(df[c], errors="coerce")
    return df


# ---------------------------------------------------------------- edycja tabeli zleceń trasy
EDITABLE = ("Zleceniodawca", "Frachty (€)", "Dodatkowe wynagrodzenie (€)", "Dyspozytor", "Płatność status", "Termin płatności")


def _txt(x) -> str:
    return "" if x is None or (isinstance(x, float) and math.isnan(x)) else str(x).strip()


def _money(x) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if math.isnan(v) or v < 0 else round(v, 2)


def apply_sheet_edits(plan: dict, edited: pd.DataFrame) -> bool:
    """Przenosi do planu edytowalne kolumny tabeli (wiersze w kolejności zleceń planu). Zwraca True, gdy coś się zmieniło."""
    changed = False
    if len(edited) != len(plan["orders"]):
        return False
    for o, rec in zip(plan["orders"], edited.to_dict("records")):
        new = {"client": _txt(rec["Zleceniodawca"]), "price_eur": _money(rec["Frachty (€)"]),
               "extra_pay_eur": _money(rec["Dodatkowe wynagrodzenie (€)"]), "dispatcher": _txt(rec["Dyspozytor"]),
               "pay_status": _txt(rec["Płatność status"]) if _txt(rec["Płatność status"]) in PAY_STATUSES else o["pay_status"],
               "pay_term_days": term_days(rec["Termin płatności"])}
        for k, v in new.items():
            if o.get(k) != v:
                o[k] = v
                changed = True
    return changed


def clean_paid_sections(rows: list[dict]) -> list[dict]:
    """Płatne odcinki z edytora (Rodzaj, Odcinek, Koszt): puste wiersze znikają, brakujący rodzaj = Prom."""
    out = []
    for r in rows:
        name, cost, kind = _txt(r.get("name")), _money(r.get("cost_eur")), _txt(r.get("kind"))
        if not (name or cost or kind):
            continue
        out.append({"kind": kind if kind in PAID_KINDS else PAID_KINDS[0], "name": name, "cost_eur": cost})
    return out
