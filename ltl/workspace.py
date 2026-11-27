"""Workspace: ręcznie układane i edytowane plany tras (warianty) dla konkretnych pojazdów.

Plan to zapis stanu pracy: zlecenia (z możliwością poprawiania danych), kolejność punktów trasy
(załadunki L, rozładunki U i dodatkowe punkty W) oraz punkty pośrednie na mapie (vias).
Wszystko jest zwykłymi słownikami/listami, żeby dało się je zapisać w bazie jako JSON.
"""
from __future__ import annotations

import copy
import math
from datetime import date, datetime

import pandas as pd

from .costs import CostParams, profit
from .geo import ROAD_FACTOR, haversine_km
from .optimizer import Stop, Variant

ORDER_FIELDS = (
    "id", "client", "source", "start_country", "start_postal", "start_city", "end_country", "end_postal", "end_city",
    "start_lat", "start_lon", "end_lat", "end_lon", "load_date", "unload_date",
    "load_from", "load_to", "unload_from", "unload_to", "weight_t", "ldm", "price_eur", "distance_km",
    "load_type", "description", "note", "dispatcher", "pay_status", "pay_term_days", "extra_pay_eur", "cancelled")
PAY_STATUSES = ("nieopłacone", "opłacone", "przeterminowane")
PAY_TERMS = (7, 15, 21, 30, 40, 45, 60)
TEXT_FIELDS = ("dispatcher", "pay_status", "id", "client", "source", "start_country", "start_postal", "start_city", "end_country", "end_postal",
               "end_city", "load_type", "description", "note")
BOOL_FIELDS = ("cancelled",)
DATE_FIELDS = ("load_date", "unload_date")
STAMP_FIELDS = ("load_from", "load_to", "unload_from", "unload_to")
NUM_FIELDS = ("pay_term_days", "extra_pay_eur", "weight_t", "ldm", "price_eur", "distance_km", "start_lat", "start_lon", "end_lat", "end_lon")
LOCATION_FIELDS = ("start_country", "start_postal", "end_country", "end_postal")


def new_plan(name: str = "") -> dict:
    return {"name": name, "note": "", "orders": [], "stops": [], "vias": [], "paid_sections": []}


def _blank(x) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x)) or x is pd.NaT or (isinstance(x, str) and not x.strip())


def _iso(x, with_time: bool = False):
    if _blank(x):
        return None
    ts = pd.Timestamp(x)
    return ts.isoformat(timespec="minutes") if with_time else ts.date().isoformat()


def _num(x):
    if _blank(x):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) else v


def clean_order(row: dict) -> dict:
    """Ujednolica zlecenie do postaci zapisywanej w planie (daty jako tekst ISO, liczby jako float, brak = None)."""
    out = {}
    for f in ORDER_FIELDS:
        v = row.get(f)
        if f in TEXT_FIELDS:
            out[f] = "" if _blank(v) else str(v).strip()
        elif f in BOOL_FIELDS:
            out[f] = bool(v) if not _blank(v) else False
        elif f in DATE_FIELDS:
            out[f] = _iso(v)
        elif f in STAMP_FIELDS:
            out[f] = _iso(v, with_time=True)
        else:
            out[f] = _num(v)
    for f in ("start_country", "end_country"):
        out[f] = out[f].upper()
    out["ldm"] = out["ldm"] or 0.0
    out["weight_t"] = out["weight_t"] or 0.0
    out["price_eur"] = out["price_eur"] or 0.0
    out["extra_pay_eur"] = out["extra_pay_eur"] or 0.0
    if out["pay_status"] not in PAY_STATUSES:
        out["pay_status"] = PAY_STATUSES[0]
    if out["pay_term_days"] is not None:
        out["pay_term_days"] = int(out["pay_term_days"])
    return out


def normalize_plan(plan: dict) -> dict:
    """Uzupełnia plan zapisany w starszej wersji (brakujące pola zleceń i planu) - bez zmiany jego treści."""
    for k, default in (("name", ""), ("note", ""), ("orders", []), ("stops", []), ("vias", []), ("start_at", None),
                       ("paid_sections", [])):
        plan.setdefault(k, default)
    plan["orders"] = [clean_order(o) for o in plan["orders"]]
    return plan


def order_revenue(o: dict) -> float:
    """Przychód zlecenia: fracht, a dla zlecenia anulowanego - tylko dodatkowe wynagrodzenie (odszkodowanie za anulowanie)."""
    return float(o.get("extra_pay_eur") or 0.0) if o.get("cancelled") else float(o.get("price_eur") or 0.0)


def order_ids(plan: dict) -> list[str]:
    return [o["id"] for o in plan["orders"]]


def _pair(oid: str) -> list[dict]:
    return [{"kind": "L", "order_id": oid}, {"kind": "U", "order_id": oid}]


def add_orders(plan: dict, rows: list[dict]) -> list[str]:
    """Dodaje zlecenia (L i U na końcu trasy). Zwraca numery faktycznie dodanych (duplikaty numerów pomija)."""
    have = set(order_ids(plan))
    added = []
    for row in rows:
        o = clean_order(row)
        if not o["id"] or o["id"] in have:
            continue
        plan["orders"].append(o)
        plan["stops"] += _pair(o["id"])
        have.add(o["id"])
        added.append(o["id"])
    return added


def remove_order(plan: dict, oid: str) -> None:
    plan["orders"] = [o for o in plan["orders"] if o["id"] != oid]
    plan["stops"] = [s for s in plan["stops"] if s.get("order_id") != oid]
    plan["vias"] = []                      # odcinki się przesunęły - punkty pośrednie przestają pasować


def next_waypoint_id(plan: dict) -> str:
    used = {s["order_id"] for s in plan["stops"] if s["kind"] == "W"}
    n = 1
    while f"P{n}" in used:
        n += 1
    return f"P{n}"


def add_waypoint(plan: dict, country: str, postal: str, city: str, lat: float, lon: float, note: str = "",
                 position: int | None = None) -> str:
    wid = next_waypoint_id(plan)
    stop = {"kind": "W", "order_id": wid, "country": country.strip().upper(), "postal": postal.strip(),
            "city": city.strip(), "lat": float(lat), "lon": float(lon), "note": note.strip()}
    if position is None or position >= len(plan["stops"]):
        plan["stops"].append(stop)
    else:
        plan["stops"].insert(max(position, 0), stop)
    plan["vias"] = []
    return wid


def remove_stop(plan: dict, index: int) -> None:
    """Punkt dodatkowy usuwa się osobno; usunięcie załadunku lub rozładunku usuwa całe zlecenie."""
    if not 0 <= index < len(plan["stops"]):
        return
    s = plan["stops"][index]
    if s["kind"] == "W":
        del plan["stops"][index]
        plan["vias"] = []
    else:
        remove_order(plan, s["order_id"])


def reorder(plan: dict, new_order: list[int]) -> None:
    """Ustawia kolejność punktów wg listy indeksów dotychczasowej kolejności (musi być permutacją)."""
    if sorted(new_order) != list(range(len(plan["stops"]))):
        raise ValueError("Kolejność musi zawierać każdy punkt dokładnie raz.")
    plan["stops"] = [plan["stops"][i] for i in new_order]
    plan["vias"] = []


def move_stop(plan: dict, index: int, delta: int) -> None:
    j = index + delta
    if not (0 <= index < len(plan["stops"]) and 0 <= j < len(plan["stops"])):
        return
    plan["stops"][index], plan["stops"][j] = plan["stops"][j], plan["stops"][index]
    plan["vias"] = []


EDITABLE_ORDER_FIELDS = tuple(f for f in ORDER_FIELDS if f not in
                              ("start_lat", "start_lon", "end_lat", "end_lon", "load_from", "load_to", "unload_from",
                               "unload_to", "source", "description", "load_type", "distance_km"))


def orders_differ(plan: dict, rows: list[dict]) -> bool:
    """Czy tabela zleceń z edytora różni się od planu (zmiana wartości, nowy lub usunięty wiersz)?
    Nowy wiersz liczy się dopiero, gdy ma kraj i kod załadunku oraz rozładunku - do tego czasu można go spokojnie
    uzupełniać (plan nie jest zapisywany ani przeliczany)."""
    old = {o["id"]: o for o in plan["orders"]}
    new = {}
    for r in rows:
        o = clean_order(r)
        if not o["id"] or o["id"] in new:
            continue
        if o["id"] not in old and not all(o[f] for f in LOCATION_FIELDS):
            continue
        new[o["id"]] = o
    if list(new) != list(old):
        return True
    return any(new[i][f] != old[i].get(f) for i in new for f in EDITABLE_ORDER_FIELDS)


def stop_edits_pending(plan: dict, numbers: list, deletes: list, notes: list | None = None) -> bool:
    """Czy tabela kolejności zawiera jakąkolwiek zmianę (numer inny niż pozycja, usunięcie, nowa uwaga)?"""
    for i, s in enumerate(plan["stops"]):
        if i < len(deletes) and deletes[i]:
            return True
        n = numbers[i] if i < len(numbers) else None
        if n is not None and n == n and float(n) != i + 1:
            return True
        if s["kind"] == "W" and notes is not None and i < len(notes) and notes[i] is not None and notes[i] == notes[i] \
                and str(notes[i]) != s.get("note", ""):
            return True
    return False


def apply_stop_edits(plan: dict, numbers: list, deletes: list, notes: list | None = None) -> set:
    """Wynik edycji tabeli kolejności: ``numbers`` = nowe numery kolejności (mniejszy = wcześniej, remis = dotychczasowa
    kolejność), ``deletes`` = punkty do usunięcia (punkt dodatkowy znika sam; załadunek/rozładunek usuwa całe zlecenie),
    ``notes`` = uwagi punktów dodatkowych. Zwraca numery usuniętych zleceń."""
    stops = plan["stops"]
    n = len(stops)
    if not (len(numbers) == len(deletes) == n):
        raise ValueError("Tabela kolejności ma inną liczbę wierszy niż plan.")
    drop_orders = {s["order_id"] for s, d in zip(stops, deletes) if d and s["kind"] != "W"}
    keep = [i for i, (s, d) in enumerate(zip(stops, deletes)) if not (d and s["kind"] == "W") and s["order_id"] not in drop_orders]
    keep.sort(key=lambda i: (float(numbers[i]) if numbers[i] is not None and numbers[i] == numbers[i] else i + 1, i))
    new_stops = []
    for i in keep:
        st_ = dict(stops[i])
        if st_["kind"] == "W" and notes is not None and notes[i] is not None and notes[i] == notes[i]:
            st_["note"] = str(notes[i])
        new_stops.append(st_)
    changed = [(x["kind"], x["order_id"]) for x in new_stops] != [(x["kind"], x["order_id"]) for x in stops]
    plan["stops"] = new_stops
    plan["orders"] = [o for o in plan["orders"] if o["id"] not in drop_orders]
    if changed:
        plan["vias"] = []
    return drop_orders


def update_orders(plan: dict, rows: list[dict], geocode=None) -> dict:
    """Wczytuje poprawioną tabelę zleceń (kolejność wierszy dowolna). Brakujące numery = usunięte zlecenia,
    nowe numery = nowe zlecenia. Gdy zmienił się kraj/kod, współrzędne są ustalane ponownie przez ``geocode(country,
    postal) -> (lat, lon)``. Zwraca {"added": [...], "removed": [...], "relocated": [...]}."""
    old = {o["id"]: o for o in plan["orders"]}
    new = []
    added, relocated, seen = [], [], set()
    for row in rows:
        o = clean_order(row)
        if not o["id"] or o["id"] in seen:               # puste i powtórzone numery pomijamy
            continue
        seen.add(o["id"])
        prev = old.get(o["id"])
        if prev is not None:                             # zmiana daty unieważnia stare okno godzinowe z oferty
            for date_f, (from_f, to_f) in (("load_date", ("load_from", "load_to")),
                                           ("unload_date", ("unload_from", "unload_to"))):
                if prev[date_f] != o[date_f]:
                    o[from_f] = o[to_f] = None
        for side in ("start", "end"):
            changed = prev is None or any(prev[f"{side}_{k}"] != o[f"{side}_{k}"] for k in ("country", "postal"))
            have_code = bool(o[f"{side}_country"] and o[f"{side}_postal"])
            if changed and not have_code:                # kod wykasowany: stare współrzędne już nie pasują
                o[f"{side}_lat"] = o[f"{side}_lon"] = None
            elif (changed or o[f"{side}_lat"] is None) and geocode is not None and have_code:
                o[f"{side}_lat"], o[f"{side}_lon"] = geocode(o[f"{side}_country"], o[f"{side}_postal"])
                if prev is not None:
                    relocated.append(o["id"])
        if prev is None:
            added.append(o["id"])
        new.append(o)
    new_ids = {o["id"] for o in new}
    removed = [i for i in old if i not in new_ids]
    plan["orders"] = new
    stops = [s for s in plan["stops"] if s["kind"] == "W" or s["order_id"] in new_ids]
    for oid in added:
        stops += _pair(oid)
    plan["stops"] = stops
    if added or removed or relocated:
        plan["vias"] = []
    return {"added": added, "removed": removed, "relocated": sorted(set(relocated))}


def _order_map(plan: dict) -> dict:
    return {o["id"]: o for o in plan["orders"]}


def validate(plan: dict, max_ldm: float = 13.6, max_t: float = 24.0) -> list[str]:
    """Problemy planu: załadunek po rozładunku, brak punktu zlecenia, przeładowanie naczepy."""
    problems = []
    omap = _order_map(plan)
    seen_l, seen_u = set(), set()
    load_ldm = load_t = peak_ldm = peak_t = 0.0
    for s in plan["stops"]:
        if s["kind"] == "W":
            continue
        o = omap.get(s["order_id"])
        if o is None:
            problems.append(f"Punkt trasy wskazuje nieistniejące zlecenie {s['order_id']}.")
            continue
        if s["kind"] == "L":
            seen_l.add(s["order_id"])
            load_ldm += o["ldm"]
            load_t += o["weight_t"]
            peak_ldm, peak_t = max(peak_ldm, load_ldm), max(peak_t, load_t)
        else:
            if s["order_id"] not in seen_l:
                problems.append(f"Zlecenie {s['order_id']}: rozładunek jest przed załadunkiem.")
            seen_u.add(s["order_id"])
            load_ldm -= o["ldm"]
            load_t -= o["weight_t"]
    for oid in omap:
        if oid not in seen_l or oid not in seen_u:
            problems.append(f"Zlecenie {oid} nie ma w trasie załadunku lub rozładunku.")
    if peak_ldm > max_ldm + 1e-9:
        problems.append(f"Przeładowanie naczepy: do {peak_ldm:.1f} LDM (limit {max_ldm:g}).")
    if peak_t > max_t + 1e-9:
        problems.append(f"Przeładowanie naczepy: do {peak_t:.1f} t (limit {max_t:g}).")
    for o in plan["orders"]:
        if o["start_lat"] is None or o["end_lat"] is None:
            problems.append(f"Zlecenie {o['id']}: nie ustalono położenia kodu pocztowego (sprawdź kraj i kod).")
    return problems


def peaks(plan: dict) -> tuple[float, float]:
    omap = _order_map(plan)
    cur_l = cur_t = pk_l = pk_t = 0.0
    for s in plan["stops"]:
        o = omap.get(s.get("order_id")) if s["kind"] != "W" else None
        if o is None:
            continue
        sign = 1 if s["kind"] == "L" else -1
        cur_l, cur_t = max(cur_l + sign * o["ldm"], 0.0), max(cur_t + sign * o["weight_t"], 0.0)
        pk_l, pk_t = max(pk_l, cur_l), max(pk_t, cur_t)
    return pk_l, pk_t


def orders_frame(plan: dict) -> pd.DataFrame:
    """Zlecenia planu jako tabela zgodna z resztą aplikacji (daty jako date/Timestamp, ceny w price_eur)."""
    rows = []
    for o in plan["orders"]:
        r = dict(o)
        for f in DATE_FIELDS:
            r[f] = date.fromisoformat(o[f]) if o[f] else None
        for f in STAMP_FIELDS:
            r[f] = datetime.fromisoformat(o[f]) if o[f] else None
        for f in NUM_FIELDS:
            r[f] = float("nan") if o[f] is None else o[f]
        pts = [o["start_lat"], o["start_lon"], o["end_lat"], o["end_lon"]]
        r["distance_est_km"] = round(float(haversine_km(*pts)) * ROAD_FACTOR) if None not in pts else float("nan")
        r["price_used_eur"] = o["price_eur"]
        for extra in ("vehicles", "body", "extras", "status", "defaults_used"):
            r.setdefault(extra, "")
        rows.append(r)
    cols = list(ORDER_FIELDS) + ["distance_est_km", "price_used_eur", "vehicles", "body", "extras", "status", "defaults_used"]
    return pd.DataFrame(rows, columns=cols)


def vehicle_start(vehicle: dict, geocode) -> Stop | None:
    """Miejsce startu pojazdu z rejestru (kraj + kod); None, gdy nie podano."""
    if not vehicle.get("start_country") or not vehicle.get("start_postal"):
        return None
    lat, lon = geocode(vehicle["start_country"], vehicle["start_postal"])
    return Stop(order_id="START", kind="S", country=vehicle["start_country"], postal=vehicle["start_postal"],
                city=vehicle.get("start_city", "") or "", lat=float(lat), lon=float(lon))


def to_variant(plan: dict, start: Stop | None, cost: CostParams) -> Variant | None:
    """Zamienia plan na Variant (ten sam typ co wyniki optymalizatora), z kosztami szacunkowymi (linia prosta × 1,3)."""
    omap = _order_map(plan)
    stops: list[Stop] = [start] if start else []
    for s in plan["stops"]:
        if s["kind"] == "W":
            stops.append(Stop(s["order_id"], "W", s["country"], s["postal"], s["city"], s["lat"], s["lon"]))
            continue
        o = omap.get(s["order_id"])
        if o is None:
            continue
        side = "start" if s["kind"] == "L" else "end"
        lat, lon = o[f"{side}_lat"], o[f"{side}_lon"]
        if lat is None or lon is None:
            return None
        stops.append(Stop(o["id"], s["kind"], o[f"{side}_country"], o[f"{side}_postal"], o[f"{side}_city"], lat, lon))
    if len(stops) < 2:
        return None
    pts = [(s.lat, s.lon) for s in stops]
    legs = [float(haversine_km(a[0], a[1], b[0], b[1])) * ROAD_FACTOR for a, b in zip(pts, pts[1:])]
    km = sum(legs)
    on_board, loaded = set(), 0.0
    if stops[0].kind == "L":
        on_board.add(stops[0].order_id)
    for leg, nxt in zip(legs, stops[1:]):
        loaded += leg if on_board else 0.0
        on_board.add(nxt.order_id) if nxt.kind == "L" else on_board.discard(nxt.order_id)
    revenue = sum(order_revenue(o) for o in plan["orders"])
    n_cargo = sum(1 for s in stops if s.kind in ("L", "U"))
    n_wp = sum(1 for s in stops if s.kind == "W")
    c = profit(revenue, km, n_cargo, cost, extra_h=n_wp * cost.waypoint_stop_h)
    pk_l, pk_t = peaks(plan)
    return Variant(order_ids=[o["id"] for o in plan["orders"]], stops=stops, revenue=revenue, cost=c,
                   profit=c["profit"], total_km=km, loaded_km=loaded, empty_km=km - loaded, detour_km=0.0,
                   days=c["days"], drive_h=c["drive_h"], peak_ldm=pk_l, peak_weight_t=pk_t)


def from_variant(variant: Variant, orders: pd.DataFrame, plan: dict | None = None) -> dict:
    """Plan z wariantu wyszukiwarki: zlecenia (z danymi z tabeli) i dokładnie ta sama kolejność punktów."""
    plan = plan or new_plan()
    by_id = {r["id"]: r for r in orders.to_dict("records")}
    rows = []
    for oid in variant.order_ids:
        r = by_id.get(oid)
        if r is not None:
            rows.append({**r, "price_eur": r.get("price_used_eur", r.get("price_eur"))})
    plan["orders"] = [clean_order(r) for r in rows]
    have = {o["id"] for o in plan["orders"]}
    stops = []
    for s in variant.stops:
        if s.kind == "S":
            continue
        if s.kind == "W":
            stops.append({"kind": "W", "order_id": s.order_id, "country": s.country, "postal": s.postal,
                          "city": s.city, "lat": s.lat, "lon": s.lon, "note": ""})
        elif s.order_id in have:
            stops.append({"kind": s.kind, "order_id": s.order_id})
    plan["stops"] = stops
    plan["vias"] = []
    return plan


def plan_signature(plan: dict) -> tuple:
    """Podpis zawartości planu (zmienia się przy każdej zmianie trasy, kosztów lub danych) - do cache tras."""
    return (tuple(tuple(sorted(copy.deepcopy(o).items(), key=lambda kv: kv[0])) for o in plan["orders"]),
            tuple(tuple(sorted(s.items())) for s in plan["stops"]),
            tuple((v["leg"], round(v["lat"], 5), round(v["lon"], 5)) for v in plan["vias"]))
