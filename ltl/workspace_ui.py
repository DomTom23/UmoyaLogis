"""Interfejs workspace: zakładki „Workspace” (plany tras per pojazd, osobiste i grupowe) oraz „Pojazdy” (rejestr)."""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass
from datetime import date, datetime, time as dtime
from typing import Callable

import pandas as pd
import streamlit as st

from .costs import CostParams
from .mapview import route_map
from .optimizer import TimeCtx
from .route_edit import Via, apply_edit, evaluate_route
from .routing import RouteResult
from .schedule import ScheduleParams, from_h, order_windows
from .storage import InvalidName, NameExists, PlanConflict, Store
from .ui import cards, empty_state, go_to, step
from .variant_detail import order_table, route_chain_html, stop_table
from . import route_sheet as rsh
from . import workspace as ws

MODES = {"👤 Praca osobista": "personal", "👥 Praca grupowa": "group"}
SECTIONS = ["📊 Tabela", "🗺️ Trasa i koszty", "🧾 Zlecenia", "🧭 Kolejność punktów", "➕ Dodaj", "⚙️ Plan", "📚 Cały pojazd"]
S_TABLE, S_ROUTE, S_ORDERS, S_STOPS, S_ADD, S_PLAN, S_FLEET = SECTIONS
WORK_TAB_INDEX = 5              # miejsce zakładki „Workspace” w ui.ALL_TABS


@dataclass
class WsContext:
    store: Store
    geocode: Callable[[str, str], tuple]            # (kraj, kod) -> (lat, lon)
    cost: CostParams
    make_sched: Callable[[datetime | None], ScheduleParams]
    get_route: Callable[[object, list], RouteResult]
    pool: pd.DataFrame | None = None                # zlecenia z bieżącego wyszukiwania (do dodawania do planu)
    est_ids: frozenset = frozenset()
    place: Callable[[str, str], str] = lambda country, postal: ""      # (kraj, kod) -> nazwa miejscowości


# ------------------------------------------------------------------ tożsamość i stan
def user_name() -> str:
    return " ".join(str(st.session_state.get("ws_user_input", "")).split())


def owner_key(name: str) -> str:
    return name.casefold()


def _sync_user_param() -> None:
    name = user_name()
    if name:
        st.query_params["u"] = name
    elif "u" in st.query_params:
        del st.query_params["u"]


def init_identity() -> None:
    """Imię z adresu strony (?u=...) - dzięki temu zakładka z linkiem „zapamiętuje” użytkownika."""
    if "ws_user_input" not in st.session_state:
        st.session_state["ws_user_input"] = st.query_params.get("u", "")


def _flash(level: str, msg: str) -> None:
    """Komunikat widoczny po następnym przeładowaniu ekranu (przeżywa dwa przebiegi skryptu, bo Streamlit potrafi
    przeładować stronę dwa razy pod rząd - np. po zmianie zakładki lub kontrolki)."""
    st.session_state.setdefault("ws_flash", []).append([level, msg, 2])


def _show_flash() -> None:
    keep = []
    for level, msg, ttl in st.session_state.get("ws_flash", []):
        getattr(st, level)(msg)
        if ttl > 1:
            keep.append([level, msg, ttl - 1])
    st.session_state["ws_flash"] = keep


def _fmt_time(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%d.%m %H:%M")
    except (TypeError, ValueError):
        return iso or "—"


# ------------------------------------------------------------------ zapis planu
class PlanCtx:
    """Wariant trasy otwarty w workspace: dane planu, wersja z bazy, kto i co edytuje."""

    def __init__(self, ctx: WsContext, scope: str, owner: str, user: str, vehicle: dict, route: dict,
                 routes: list[dict], row: dict):
        self.ctx, self.scope, self.owner, self.user, self.vehicle = ctx, scope, owner, user, vehicle
        self.route, self.routes, self.row = route, routes, row
        self.vid = row["id"]
        self.kid = f"{scope}_{self.vid}"                    # unikalny dla wariantu - do kluczy kontrolek
        self.plan = ws.normalize_plan(copy.deepcopy(row["data"]))
        self.version = row["version"]
        self.basekey = f"ws_base_{self.kid}"
        base = st.session_state.get(self.basekey)
        self.stale = base is not None and base != self.version      # ktoś zapisał zmiany, od kiedy ekran został narysowany
        # Licznik odświeżeń tabel edycji: zmienia się po każdym zapisie i gdy plan zmienił ktoś inny (tabele pokazują
        # wtedy świeże dane). Klucze formularzy są stałe - inaczej wysłanie nieaktualnego formularza przepadałoby po cichu.
        self.nonce_key = f"ws_nonce_{self.kid}"
        self.nonce = st.session_state.get(self.nonce_key, 0) + (1 if self.stale else 0)
        st.session_state[self.nonce_key] = self.nonce

    def _conflict(self, current: dict | None) -> None:
        if current is None:
            _flash("warning", "Ten wariant został w międzyczasie usunięty przez inną osobę.")
        else:
            who = current.get("updated_by") or "inna osoba"
            _flash("warning", f"Wariant został w międzyczasie zmieniony ({who}, {_fmt_time(current.get('updated_at', ''))}). "
                              "Wczytano najnowszą wersję - ponów swoją zmianę.")
            st.session_state[self.basekey] = current.get("version", 0)
            st.session_state[self.nonce_key] = self.nonce + 1
        st.rerun()

    def apply(self, mutate: Callable[[dict], object], message: str = "") -> None:
        """Wykonuje zmianę planu i od razu zapisuje ją w bazie (z kontrolą wersji)."""
        if self.stale:
            self._conflict(self.row)
        mutate(self.plan)
        try:
            self.version = self.ctx.store.save_variant(self.vid, self.plan, self.version, self.user)
        except PlanConflict as e:
            self._conflict(e.current)
        st.session_state[self.basekey] = self.version
        st.session_state[self.nonce_key] = self.nonce + 1
        if message:
            _flash("success", message)
        st.rerun()

    def mark_seen(self) -> None:
        st.session_state[self.basekey] = self.version


# ------------------------------------------------------------------ zakładka „Pojazdy”
VEHICLE_COLUMNS = {"name": "Nazwa / numer rejestracyjny", "note": "Uwagi", "body": "Nadwozie", "max_ldm": "Maks. LDM",
                   "max_weight_t": "Ładowność (t)", "start_country": "Start: kraj", "start_postal": "Start: kod",
                   "start_city": "Start: miasto"}


def render_vehicles_tab(ctx: WsContext) -> None:
    step("Pojazdy", "Lista pojazdów firmy",
         "Z tej listy wybierasz auto w zakładce Workspace. Miejsce startu pojazdu (kraj i kod) jest początkiem jego trasy, "
         "a LDM i ładowność - limitem sprawdzanym przy układaniu planu.")
    veh = ctx.store.vehicles()
    base = veh.drop(columns=["active", "id"]) if len(veh) else pd.DataFrame(columns=list(VEHICLE_COLUMNS))
    show = base.reset_index(drop=True).rename(columns=VEHICLE_COLUMNS)
    edited = st.data_editor(
        show, key="veh_editor", num_rows="dynamic", hide_index=True, width="stretch",
        column_config={
            VEHICLE_COLUMNS["name"]: st.column_config.TextColumn(VEHICLE_COLUMNS["name"], required=True, max_chars=80, width=190),
            VEHICLE_COLUMNS["note"]: st.column_config.TextColumn(VEHICLE_COLUMNS["note"], width=200),
            VEHICLE_COLUMNS["body"]: st.column_config.TextColumn(VEHICLE_COLUMNS["body"], width=110, help="np. firanka, mega"),
            VEHICLE_COLUMNS["max_ldm"]: st.column_config.NumberColumn(VEHICLE_COLUMNS["max_ldm"], min_value=1.0, max_value=20.0,
                                                                      format="%.1f", default=13.6, width=100),
            VEHICLE_COLUMNS["max_weight_t"]: st.column_config.NumberColumn(VEHICLE_COLUMNS["max_weight_t"], min_value=1.0,
                                                                           max_value=60.0, format="%.1f", default=24.0, width=110),
            VEHICLE_COLUMNS["start_country"]: st.column_config.TextColumn(VEHICLE_COLUMNS["start_country"], max_chars=2, width=95),
            VEHICLE_COLUMNS["start_postal"]: st.column_config.TextColumn(VEHICLE_COLUMNS["start_postal"], width=100),
            VEHICLE_COLUMNS["start_city"]: st.column_config.TextColumn(VEHICLE_COLUMNS["start_city"], width=130)})
    st.caption("Nowy pojazd dodasz w ostatnim, pustym wierszu. Usunięcie wiersza wycofuje pojazd z listy "
               "(jego plany zostają w bazie i wrócą po przywróceniu).")
    if st.button("💾 Zapisz listę pojazdów", type="primary", key="veh_save"):
        st.session_state["veh_flash"] = save_vehicles(ctx.store, veh, edited)
        st.rerun()
    for level, msg in st.session_state.pop("veh_flash", []):
        getattr(st, level)(msg)
    old = ctx.store.vehicles(active_only=False)
    retired = old[old["active"] == 0]
    if len(retired):
        with st.expander(f"Wycofane pojazdy ({len(retired)})"):
            for r in retired.itertuples():
                c1, c2, c3 = st.columns([4, 1.2, 1.6])
                c1.write(f"**{r.name}** {r.note or ''}")
                if c2.button("Przywróć", key=f"veh_restore_{r.id}"):
                    ctx.store.save_vehicle(r.id)
                    st.rerun()
                if c3.button("Usuń na stałe", key=f"veh_del_{r.id}", help="Kasuje pojazd razem z jego planami."):
                    ctx.store.delete_vehicle(r.id)
                    st.rerun()


def save_vehicles(store: Store, before: pd.DataFrame, edited: pd.DataFrame) -> list[tuple[str, str]]:
    """Zapisuje zmiany z edytora pojazdów (nowe, zmienione, usunięte = wycofane). Zwraca komunikaty (poziom, tekst)."""
    inv = {v: k for k, v in VEHICLE_COLUMNS.items()}
    edited = edited.rename(columns=inv)
    ids_before = list(before["id"]) if len(before) else []
    # data_editor zwraca wiersze w kolejności wyświetlania: pierwsze len(before) odpowiadają dotychczasowym pojazdom,
    # ale wiersze mogły zostać usunięte - dlatego dopasowujemy po indeksie oryginału, który data_editor zachowuje
    kept = {}
    msgs: list[tuple[str, str]] = []
    n_new = n_upd = 0
    for idx, row in edited.iterrows():
        name = row.get("name")
        if pd.isna(name) or not str(name).strip():
            continue
        fields = {k: row.get(k) for k in VEHICLE_COLUMNS if k in edited.columns}
        for k in ("max_ldm", "max_weight_t"):
            if pd.isna(fields.get(k)):
                fields[k] = 13.6 if k == "max_ldm" else 24.0
        for k in ("note", "body", "start_country", "start_postal", "start_city"):
            fields[k] = "" if pd.isna(fields.get(k)) else str(fields[k])
        vid = ids_before[idx] if isinstance(idx, int) and idx < len(ids_before) and idx not in kept else None
        try:
            if vid is None:
                store.save_vehicle(None, **fields)
                n_new += 1
            else:
                store.save_vehicle(vid, **fields)
                kept[idx] = vid
                n_upd += 1
        except NameExists:
            msgs.append(("warning", f"Pojazd „{str(name).strip()}” już istnieje - zmień nazwę."))
        except InvalidName as e:
            msgs.append(("warning", str(e)))
    removed = [vid for i, vid in enumerate(ids_before) if i not in kept]
    for vid in removed:
        store.retire_vehicle(vid)
    msgs.append(("success", f"Zapisano pojazdy: nowe {n_new}, zaktualizowane {n_upd}, wycofane {len(removed)}."))
    return msgs


# ------------------------------------------------------------------ zakładka „Workspace”
def _vehicle_label(v, overview: dict) -> str:
    info = overview.get(v.id)
    if not info:
        return v.name
    return f"{v.name} · tras: {info['n_routes']} · zlec.: {info['n_orders']}"


def _route_label(r: dict, variants: list[dict]) -> str:
    active = next((x for x in variants if x["id"] == r["active_variant_id"]), variants[0] if variants else None)
    n = f"{active['n_orders']} zlec." if active else "pusta"
    return f"{r['code']} · {len(variants)} war. · {n}"


def _new_route(ctx: WsContext, scope: str, owner: str, vid: int, user: str) -> None:
    rid, var = ctx.store.create_route(scope, owner, vid, by=user)
    st.session_state[f"ws_route_{scope}_{owner}_{vid}"] = rid
    st.session_state[f"ws_variant_{rid}"] = var
    st.session_state["ws_section"] = S_ADD
    _flash("success", "Utworzono nową trasę. Dodaj do niej zlecenia.")


def _new_variant(ctx: WsContext, rid: int, user: str) -> None:
    cur = ctx.store.get_variant(st.session_state.get(f"ws_variant_{rid}", 0))
    data = copy.deepcopy(cur["data"]) if cur else None
    var = ctx.store.add_variant(rid, data, by=user)
    st.session_state[f"ws_variant_{rid}"] = var
    _flash("success", "Dodano wariant (kopia poprzedniego). Zmieniaj go swobodnie - oryginał zostaje bez zmian.")


def _activate_variant(ctx: WsContext, rid: int) -> None:
    ctx.store.set_active_variant(rid, st.session_state.get(f"ws_variant_{rid}", 0))
    _flash("success", "Ten wariant jest teraz aktywny (liczy się w arkuszu całego pojazdu).")


def render_workspace_tab(ctx: WsContext) -> None:
    init_identity()
    step("Workspace", "Auta → trasy → warianty",
         "Wybierz auto, jego trasę i wariant - dla każdego wariantu powstaje tabela tras i zleceń z kosztami i marżą. "
         "Każda zmiana zapisuje się od razu. Praca osobista jest widoczna tylko dla Ciebie (po imieniu), grupowa - dla całej firmy.")
    _show_flash()
    c1, c2 = st.columns([3, 2])
    st.session_state.setdefault("ws_mode", list(MODES)[0])
    mode_label = c1.segmented_control("Tryb pracy", list(MODES), key="ws_mode",
                                      help="Osobista: własne trasy, których nie widzą inni. Grupowa: wspólne trasy, "
                                           "które może edytować każdy pracownik.") or list(MODES)[0]
    scope = MODES[mode_label]
    c2.text_input("Twoje imię lub inicjały", key="ws_user_input", on_change=_sync_user_param,
                  placeholder="np. Anna K.",
                  help="Pracę osobistą rozpoznajemy po imieniu (bez hasła). W pracy grupowej imię widać przy ostatniej zmianie. "
                       "Imię zapamiętuje się w adresie strony - zapisz stronę w zakładkach przeglądarki.")
    user = user_name()
    if scope == "personal" and not user:
        empty_state("👤", "Wpisz swoje imię (pole powyżej), aby korzystać z pracy osobistej.")
        return
    vehicles = ctx.store.vehicles()
    if vehicles.empty:
        empty_state("🚛", "Najpierw dodaj pojazdy w zakładce „🚛 Pojazdy”.")
        return
    owner = owner_key(user) if scope == "personal" else ""
    overview = ctx.store.routes_overview(scope, owner)
    labels = {v.id: _vehicle_label(v, overview) for v in vehicles.itertuples()}
    ids = list(labels)
    if st.session_state.get("ws_vehicle") not in ids:
        st.session_state["ws_vehicle"] = ids[0]
    vid = st.selectbox("Auto", ids, key="ws_vehicle", format_func=labels.get,
                       help="Dla każdego auta jest osobna lista tras w danym trybie pracy.")
    vehicle = vehicles[vehicles["id"] == vid].iloc[0].to_dict()

    # --- trasy auta
    routes = ctx.store.list_routes(scope, owner, vid)
    if not routes:
        empty_state("🧭", f"Auto {vehicle['name']} nie ma jeszcze tras.")
        st.button("➕ Utwórz pierwszą trasę", key="ws_first_route", type="primary",
                  on_click=_new_route, args=(ctx, scope, owner, vid, user or "—"))
        return
    rkey = f"ws_route_{scope}_{owner}_{vid}"
    route_ids = [r["id"] for r in routes]
    if st.session_state.get(rkey) not in route_ids:
        st.session_state[rkey] = route_ids[0]
    variants_of = {r["id"]: ctx.store.list_variants(r["id"]) for r in routes}
    rlabels = {r["id"]: _route_label(r, variants_of[r["id"]]) for r in routes}
    r1, r2, r3, r4, r5 = st.columns([5, 1.6, 1.2, 0.6, 0.6])
    rid = r1.selectbox("Trasa", route_ids, key=rkey, format_func=rlabels.get,
                       help="Trasy auta w kolejności jazdy: koniec poprzedniej jest początkiem następnej.")
    route = next(r for r in routes if r["id"] == rid)
    r2.button("➕ Nowa trasa", key="ws_new_route", on_click=_new_route, args=(ctx, scope, owner, vid, user or "—"),
              help="Pusta trasa na końcu listy.")
    with r3.popover("🗑 Usuń"):
        st.caption(f"Usunąć trasę {route['code']} razem z wszystkimi jej wariantami?")
        if st.button("Tak, usuń trasę", key=f"ws_del_route_{rid}", type="primary"):
            ctx.store.delete_route(rid)
            st.rerun()
    pos = route_ids.index(rid)
    if r4.button("↑", key="ws_route_up", disabled=pos == 0, help="Przesuń trasę wcześniej"):
        ctx.store.move_route(rid, -1)
        st.rerun()
    if r5.button("↓", key="ws_route_down", disabled=pos == len(route_ids) - 1, help="Przesuń trasę później"):
        ctx.store.move_route(rid, +1)
        st.rerun()

    # --- warianty trasy
    variants = variants_of[rid]
    vids = [x["id"] for x in variants]
    vkey = f"ws_variant_{rid}"
    if st.session_state.get(vkey) not in vids:
        st.session_state[vkey] = route["active_variant_id"] if route["active_variant_id"] in vids else vids[0]
    vlabels = {x["id"]: f"{x['label'] or 'Wariant'}{' ⭐' if x['id'] == route['active_variant_id'] else ''} ({x['n_orders']})"
               for x in variants}
    v1, v2, v3, v4 = st.columns([5, 1.8, 1.7, 1.2])
    selected = v1.radio("Wariant", vids, key=vkey, format_func=vlabels.get, horizontal=True,
                        help="⭐ = wariant aktywny (liczony w arkuszu całego auta). W nawiasie liczba zleceń.")
    v2.button("➕ Nowy wariant", key=f"ws_new_var_{rid}", on_click=_new_variant, args=(ctx, rid, user or "—"),
              help="Kopia wybranego wariantu - do przetestowania innego ułożenia.")
    v3.button("⭐ Ustaw aktywny", key=f"ws_act_var_{rid}", on_click=_activate_variant, args=(ctx, rid),
              disabled=selected == route["active_variant_id"])
    with v4.popover("🗑 Usuń", help="Usuń wybrany wariant"):
        if len(vids) < 2:
            st.caption("Trasa musi mieć co najmniej jeden wariant.")
        else:
            st.caption("Usunąć wybrany wariant?")
            if st.button("Tak, usuń wariant", key=f"ws_del_var_{selected}", type="primary"):
                ctx.store.delete_variant(selected)
                st.rerun()
    row = ctx.store.get_variant(selected)
    if row is None:
        st.rerun()
    pc = PlanCtx(ctx, scope, owner, user or "—", vehicle, route, routes, row)
    i1, i2 = st.columns([5, 1])
    i1.caption(f"{'Osobista' if scope == 'personal' else 'Grupowa'} trasa **{route['code']}** auta **{vehicle['name']}** · "
               f"wariant „{row['label']}” · ostatnia zmiana: {row['updated_by'] or '—'}, {_fmt_time(row['updated_at'])} · "
               f"wersja {pc.version} · limit {vehicle['max_ldm']:g} LDM / {vehicle['max_weight_t']:g} t")
    if i2.button("🔄 Odśwież", key="ws_refresh", help="Wczytaj najnowszy stan z bazy (zmiany innych osób)."):
        pc.mark_seen()
        st.rerun()
    default_section = S_TABLE if pc.plan["orders"] else S_ADD
    st.session_state.setdefault("ws_section", default_section)
    section = st.segmented_control("Sekcja", SECTIONS, key="ws_section", label_visibility="collapsed") or default_section
    if section == S_FLEET:
        _section_fleet(pc)
        pc.mark_seen()
        return
    e, why, problems = _evaluate(pc)                 # koszty i trasa liczone po każdej zmianie, w każdej sekcji
    for prob in problems:
        st.warning(prob)
    if e is not None and section != S_TABLE:
        _cost_strip(pc, e)
    if section == S_TABLE:
        _section_table(pc, e, why)
    elif section == S_ROUTE:
        _section_route(pc, e, why)
    elif section == S_ORDERS:
        _section_orders(pc)
    elif section == S_STOPS:
        _section_stops(pc)
    elif section == S_ADD:
        _section_add(pc)
    else:
        _section_settings(pc)
    pc.mark_seen()


# --- wyliczenia planu (trasa, koszty, harmonogram) - wspólne dla wszystkich sekcji
def _vias(plan: dict) -> list[Via]:
    return [Via(id=i + 1, leg=int(v["leg"]), lat=float(v["lat"]), lon=float(v["lon"])) for i, v in enumerate(plan["vias"])]


@dataclass
class Eval:
    v: object
    route: RouteResult
    ev: dict
    sch: object
    frame: pd.DataFrame
    vias: list


def _evaluate_plan(ctx: WsContext, plan: dict, vehicle: dict):
    """(Eval | None, powód braku, lista problemów). Trasy są w pamięci podręcznej, więc ponowne wyznaczanie
    następuje tylko po zmianie punktów. Płatne odcinki z planu (prom/pociąg/…) zwiększają koszty i obniżają zysk."""
    problems = ws.validate(plan, vehicle["max_ldm"], vehicle["max_weight_t"]) if (plan["orders"] or plan["stops"]) else []
    if not plan["orders"] and not plan["stops"]:
        return None, "empty", problems
    start = ws.vehicle_start(vehicle, ctx.geocode)
    v = ws.to_variant(plan, start, ctx.cost)
    if v is None:
        return None, "few", problems
    start_at = datetime.fromisoformat(plan["start_at"]) if plan.get("start_at") else None
    sched = ctx.make_sched(start_at)
    frame = ws.orders_frame(plan)
    time_ctx = TimeCtx(sched, order_windows(frame, sched))
    vias = _vias(plan)
    route = ctx.get_route(v, vias)
    ev = evaluate_route(v, route, ctx.cost, time_ctx)
    manual = rsh.paid_total(plan)
    if manual:
        ev["total"] += manual
        ev["profit"] -= manual
        ev["fees"] += manual
    ev["manual_fees"] = manual
    return Eval(v, route, ev, ev["schedule"], frame, vias), "", problems


def _evaluate(pc: PlanCtx):
    with st.spinner("Przeliczanie trasy i kosztów…"):
        return _evaluate_plan(pc.ctx, pc.plan, pc.vehicle)


def _cost_strip(pc: PlanCtx, e: Eval) -> None:
    v, ev, sch, vehicle, plan = e.v, e.ev, e.sch, pc.vehicle, pc.plan
    d_est = ev["profit"] - v.profit
    cards([
        ("Zysk", f"{ev['profit']:,.0f} EUR".replace(",", " "), f"{d_est:+,.0f} EUR vs szacunek".replace(",", " "),
         "pos" if ev["profit"] >= 0 else "neg", "Przychód minus koszty operacyjne."),
        ("Przychód", f"{ev['revenue']:,.0f} EUR".replace(",", " "), None, "", "Suma cen zleceń w planie."),
        ("Koszty", f"{ev['total']:,.0f} EUR".replace(",", " "), None, "", "Paliwo + myto + praca + przeprawy."),
        ("Km pełne / puste", f"{ev['loaded_km']:,.0f} / {ev['empty_km']:,.0f}".replace(",", " "), None, "",
         "Pełne: z ładunkiem. Puste: bez ładunku."),
        ("Czas", f"{ev['days']:.1f} dnia", f"jazda {ev['drive_h']:.1f} h", "", "Dni pracy z limitem 9 h dziennie."),
        ("Terminy", "✓ w oknach" if sch.feasible else f"⚠ +{sch.max_late_h:.1f} h",
         f"{sch.total_h / 24:.1f} dnia od wyjazdu", "pos" if sch.feasible else "neg",
         "Harmonogram z oknami czasowymi, limitami jazdy i odpoczynkami."),
        ("Naczepa", f"{v.peak_ldm:.1f} / {vehicle['max_ldm']:g} LDM", f"{v.peak_weight_t:.1f} / {vehicle['max_weight_t']:g} t",
         "pos" if v.peak_ldm <= vehicle["max_ldm"] + 1e-9 and v.peak_weight_t <= vehicle["max_weight_t"] + 1e-9 else "neg",
         "Największe zapełnienie w trakcie trasy."),
    ])
    def n(x: float) -> str:
        return f"{x:,.0f}".replace(",", " ")
    extra = (f" + przeprawy {n(ev['fees'])}" if ev["fees"] else "") + (f" + czekanie {n(ev['idle_cost'])}" if ev["idle_cost"] else "")
    st.caption(f"Koszty: paliwo {n(ev['fuel'])} + myto {n(ev['tolls'])} + praca {n(ev['labor'])}{extra} EUR. "
               f"Harmonogram: wyjazd {from_h(sch.start_h):%d.%m %H:%M} → koniec {from_h(sch.end_h):%d.%m %H:%M} "
               f"(czekanie {sch.wait_h:.0f} h, odpoczynki {sch.rest_h:.0f} h)."
               + ("" if plan.get("start_at") else " Wyjazd automatyczny (datę ustawisz w sekcji „⚙️ Plan”)."))


# --- sekcja: tabela trasy i zleceń (układ arkusza)
def _money_col(label: str, fmt: str = "%.2f") -> "st.column_config.NumberColumn":
    return st.column_config.NumberColumn(label, format=fmt)


def _sheet_config(editable: bool) -> dict:
    cfg = {
        "Nr trasy/zlecenia": st.column_config.TextColumn("Nr trasy/zlecenia", width=170, pinned=True),
        "Skąd": st.column_config.TextColumn("Skąd", width=140), "Dokąd": st.column_config.TextColumn("Dokąd", width=140),
        "Dni trasy": st.column_config.NumberColumn("Dni trasy", format="%d", width=70),
        "Start/Załadunek": st.column_config.TextColumn("Start/Załadunek", width=105),
        "Koniec/Rozładunek": st.column_config.TextColumn("Koniec/Rozładunek", width=115),
        "Km": st.column_config.NumberColumn("Km", format="%d", width=70),
        "Płatność do": st.column_config.TextColumn("Płatność do", width=100)}
    for label in ("Frachty (€)", "Dodatkowe wynagrodzenie (€)", "Łącznie (€)", "Marża łącznie (€)", "Koszt razem (€)",
                  "Koszt dzienny (€)", "Paliwo (€)", "Opłaty drogowe (€)", "Prom/tunel (€)"):
        cfg[label] = st.column_config.NumberColumn(label, format="%.2f", width=105, **({"min_value": 0.0} if editable and label in rsh.EDITABLE else {}))
    for label in ("Stawka €/km", "€/LDM", "Marża €/km"):
        cfg[label] = st.column_config.NumberColumn(label, format="%.2f", width=85)
    cfg["LDM"] = st.column_config.NumberColumn("LDM", format="%.1f", width=65)
    cfg["Waga (t)"] = st.column_config.NumberColumn("Waga (t)", format="%.1f", width=75)
    cfg["Zleceniodawca"] = st.column_config.TextColumn("Zleceniodawca", width=140)
    cfg["Dyspozytor"] = st.column_config.TextColumn("Dyspozytor", width=115)
    cfg["Płatność status"] = st.column_config.SelectboxColumn("Płatność status", options=list(ws.PAY_STATUSES), width=125,
                                                              help="Nieopłacone, opłacone albo przeterminowane.")
    cfg["Termin płatności"] = st.column_config.SelectboxColumn("Termin płatności", options=rsh.TERM_LABELS, width=115,
                                                               help="Liczony od daty rozładunku zlecenia.")
    return cfg


def _marked(frame: pd.DataFrame) -> pd.DataFrame:
    """Wiersze tras oznaczone „▶” (wiersze zleceń: numer kończy się „-01”, „-02”…)."""
    out = frame.copy()
    is_order = out["Nr trasy/zlecenia"].astype(str).str.contains(r"-\d{4}-\d{2}$")
    out["Nr trasy/zlecenia"] = [c if o else f"▶ {c}" for c, o in zip(out["Nr trasy/zlecenia"], is_order)]
    return out


def _vehicle_start_place(pc_or_vehicle, ctx: WsContext) -> str:
    v = pc_or_vehicle
    return rsh.place_label(v.get("start_country", ""), v.get("start_postal", ""), v.get("start_city", ""), ctx.place)


def _prev_end(ctx: WsContext, vehicle: dict, routes: list[dict], route: dict) -> dict:
    """Koniec poprzedniej trasy auta (jej aktywnego wariantu); dla pierwszej trasy - miejsce startu auta."""
    prev = None
    for r in routes:
        if r["id"] == route["id"]:
            break
        prev = r
    if prev and prev["active_variant_id"]:
        row = ctx.store.get_variant(prev["active_variant_id"])
        if row:
            end = rsh.end_of(ws.normalize_plan(row["data"]), ctx.place)
            if end["place"] != "—":
                return end
    return {"place": _vehicle_start_place(vehicle, ctx) if vehicle.get("start_country") else "—", "date": None}


def _calendar_days(e: Eval | None) -> int | None:
    return int(math.ceil(e.sch.total_h / 24.0 - 1e-9)) if e is not None and e.sch.total_h > 0 else None


def _section_table(pc: PlanCtx, e: Eval | None, why: str) -> None:
    ctx, plan = pc.ctx, pc.plan
    if not plan["orders"]:
        empty_state("📊", "Wariant jest pusty. Dodaj zlecenia w sekcji „➕ Dodaj” albo wyślij tu wariant z zakładki „5 · Trasa”.")
        return
    if e is None:
        st.info("Nie da się jeszcze policzyć kosztów trasy (za mało punktów albo brak współrzędnych) - "
                "w tabeli są tylko dane ze zleceń.")
    rsh.warm_places([plan], ctx.place)
    prev = _prev_end(ctx, pc.vehicle, pc.routes, pc.route)
    route_row, order_rows, _ = rsh.build_sheet(pc.route["code"], plan, e.ev if e else None, e.route.km if e else None,
                                               _calendar_days(e), prev, ctx.place)
    if e is not None:
        _cost_strip(pc, e)
    st.markdown(f"**Trasa {pc.route['code']}** - podsumowanie całej trasy")
    st.dataframe(_marked(rsh.sheet_frame([route_row])), hide_index=True, width="stretch", column_config=_sheet_config(False))
    st.markdown("**Zlecenia na trasie** - kolumny z ikoną ✎ (zleceniodawca, frachty, dodatkowe wynagrodzenie, dyspozytor, płatność, termin) edytujesz w tabeli; zapis i przeliczenie od razu")
    frame = rsh.sheet_frame(order_rows)
    edited = st.data_editor(
        frame, key=f"ws_sheet_{pc.kid}_{pc.nonce}", hide_index=True, width="stretch", num_rows="fixed",
        disabled=[c for c in rsh.COLUMNS if c not in rsh.EDITABLE], column_config=_sheet_config(True))
    probe = copy.deepcopy(plan)
    if rsh.apply_sheet_edits(probe, edited):
        pc.apply(lambda p: rsh.apply_sheet_edits(p, edited), "Zapisano zmiany w zleceniach.")
    st.caption("Wiersz zlecenia: Km z oferty (albo szacunek z mapy), marża zlecenia = jego przychód minus część kosztów trasy "
               "proporcjonalna do km (suma marż zleceń = marża trasy). Skąd/Dokąd: kraj i miejscowość odczytana z kodu "
               "pocztowego. Termin płatności liczy się od daty rozładunku.")

    st.markdown("**Płatne odcinki trasy** (kolumna Prom/tunel)")
    crossings = e.ev["crossings"] if e is not None else []
    auto = [c for c in crossings if c.kind in ("channel_tunnel", "channel_ferry", "alpine_tunnel")]
    if auto:
        st.caption("Wykryte na trasie i wliczone automatycznie: " + "; ".join(c.label for c in auto)
                   + ". Dodaj poniżej własne płatne odcinki (prom, pociąg, tunel, inne) - ich koszt zwiększa Prom/tunel i koszty trasy.")
    else:
        st.caption("Na trasie nie wykryto płatnych przepraw. Jeśli trasa ma prom, pociąg lub inny płatny odcinek - wybierz go z listy.")
    cols = {"kind": "Rodzaj", "name": "Odcinek / opis", "cost_eur": "Koszt (€)"}
    pdf = pd.DataFrame(plan["paid_sections"], columns=list(cols)).rename(columns=cols)
    pedit = st.data_editor(
        pdf, key=f"ws_paid_{pc.kid}_{pc.nonce}", num_rows="dynamic", hide_index=True, width="stretch",
        column_config={"Rodzaj": st.column_config.SelectboxColumn("Rodzaj", options=list(rsh.PAID_KINDS), width=120,
                                                                   help="Prom, pociąg (np. Eurotunel), tunel albo inny płatny odcinek."),
                       "Odcinek / opis": st.column_config.TextColumn("Odcinek / opis", width=260),
                       "Koszt (€)": st.column_config.NumberColumn("Koszt (€)", min_value=0.0, format="%.2f", width=110)})
    paid = rsh.clean_paid_sections(pedit.rename(columns={v: k for k, v in cols.items()}).to_dict("records"))
    if paid != plan["paid_sections"]:
        pc.apply(lambda p: p.__setitem__("paid_sections", paid), "Zapisano płatne odcinki.")


def _section_fleet(pc: PlanCtx) -> None:
    """Arkusz całego auta: aktywny wariant każdej trasy, trasy po kolei (koniec jednej = początek następnej)."""
    ctx, vehicle = pc.ctx, pc.vehicle
    st.caption("Wszystkie trasy auta w kolejności jazdy - dla każdej liczony jest jej aktywny wariant (⭐).")
    plans = []
    for r in pc.routes:
        row = ctx.store.get_variant(r["active_variant_id"]) if r["active_variant_id"] else None
        plans.append((r, ws.normalize_plan(row["data"]) if row else ws.new_plan()))
    rsh.warm_places([p for _, p in plans], ctx.place)
    rows, prev = [], {"place": _vehicle_start_place(vehicle, ctx) if vehicle.get("start_country") else "—", "date": None}
    totals = {"rev": 0.0, "profit": 0.0, "km": 0.0}
    with st.spinner("Przeliczanie tras auta…"):
        for r, plan in plans:
            e = _evaluate_plan(ctx, plan, vehicle)[0] if plan["orders"] else None
            route_row, order_rows, end = rsh.build_sheet(r["code"], plan, e.ev if e else None, e.route.km if e else None,
                                                         _calendar_days(e), prev, ctx.place)
            rows += [route_row] + order_rows
            if plan["orders"]:
                prev = end if end["place"] != "—" else prev
            if e is not None:
                totals["rev"] += route_row["Łącznie (€)"]
                totals["profit"] += route_row["Marża łącznie (€)"] or 0.0
                totals["km"] += route_row["Km"] or 0
    if not any(plan["orders"] for _, plan in plans):
        empty_state("📚", "Trasy tego auta nie mają jeszcze zleceń.")
        return
    cards([("Przychód tras", f"{totals['rev']:,.0f} EUR".replace(",", " "), None, "", "Suma kolumny Łącznie tras z policzoną trasą."),
           ("Marża łącznie", f"{totals['profit']:,.0f} EUR".replace(",", " "), None, "pos" if totals["profit"] >= 0 else "neg",
            "Suma marż tras (przychód minus koszty)."),
           ("Km razem", f"{totals['km']:,.0f}".replace(",", " "), None, "", "Suma km tras.")])
    st.dataframe(_marked(rsh.sheet_frame(rows)), hide_index=True, width="stretch", column_config=_sheet_config(False))


# --- sekcja: trasa i koszty (mapa, przebieg, tabele)
def _section_route(pc: PlanCtx, e: Eval | None, why: str) -> None:
    ctx, plan, vehicle = pc.ctx, pc.plan, pc.vehicle
    if e is None:
        if why == "empty":
            empty_state("🧰", "Plan jest pusty. Dodaj zlecenia w sekcji „➕ Dodaj” albo wyślij tu wariant z zakładki „5 · Trasa”.")
        else:
            empty_state("📍", "Za mało punktów do wyznaczenia trasy (potrzebne co najmniej dwa) albo brak współrzędnych kodów.")
        return
    v, route, sch, frame, vias = e.v, e.route, e.sch, e.frame, e.vias
    if ws.vehicle_start(vehicle, ctx.geocode) is None:
        st.info("Pojazd nie ma miejsca startu (zakładka „🚛 Pojazdy”) - trasa jest liczona od pierwszego punktu.")
    st.markdown("<div class='chain'>" + route_chain_html(v) + "</div>", unsafe_allow_html=True)
    map_id = f"ws-{pc.kid}-{pc.version}"
    event = route_map(v, route, vias, map_id=map_id, fit_id=map_id, height=560, key=f"ws_map_{pc.kid}")
    seen = st.session_state.setdefault("ws_seen_nonce", set())
    if event and event.get("map_id") == map_id and event["nonce"] not in seen:
        seen.add(event["nonce"])
        new = apply_edit(vias, event, route)

        def mutate(p, new=new):
            p["vias"] = [{"leg": x.leg, "lat": x.lat, "lon": x.lon} for x in new]
        pc.apply(mutate)
    if vias:
        st.caption(f"Punkty pośrednie: {len(vias)} (zapisane w planie).")
    if not route.has_real:
        st.warning(f"Brak routingu po drogach ({route.error or 'serwer niedostępny'}). Trasa jest orientacyjna (linia prosta).")
    elif route.straight_legs:
        st.warning("Odcinki " + ", ".join(str(i + 1) for i in route.straight_legs) + " liczone linią prostą - serwer nie wyznaczył drogi.")
    else:
        st.success(f"Trasa po drogach dla ciężarówek ({route.provider}): {route.km:,.0f} km, {route.hours:.1f} h jazdy.")
    if not sch.feasible:
        late = [f"{x.order_id} {'załadunek' if x.kind == 'L' else 'rozładunek'} (+{x.late_h:.1f} h)"
                for x in sch.stops if x.late_h > 1e-9]
        st.warning("⏰ Plan nie mieści się w oknach czasowych: " + "; ".join(late) + ".")
    stops_df = stop_table(v, route, frame, sch)
    if (stops_df["Zleceniodawca"] == "—").all():
        stops_df = stops_df.drop(columns="Zleceniodawca")
    t1, t2 = st.tabs(["Przebieg krok po kroku", "Zlecenia w planie"])
    with t1:
        st.dataframe(stops_df, width="stretch", hide_index=True)
    with t2:
        od = order_table(v, frame.assign(price_eur=frame["price_eur"]), set(ctx.est_ids))
        st.dataframe(od, width="stretch", hide_index=True)


# --- sekcja: zlecenia (edycja danych)
ORDER_EDIT_COLUMNS = [
    ("id", "Nr", "text"), ("client", "Zleceniodawca", "text"),
    ("start_country", "Zał. kraj", "text"), ("start_postal", "Zał. kod", "text"), ("start_city", "Zał. miasto", "text"),
    ("load_date", "Data zał.", "date"),
    ("end_country", "Rozł. kraj", "text"), ("end_postal", "Rozł. kod", "text"), ("end_city", "Rozł. miasto", "text"),
    ("unload_date", "Data rozł.", "date"),
    ("ldm", "LDM", "num"), ("weight_t", "Waga (t)", "num"), ("price_eur", "Cena (EUR)", "num"), ("note", "Uwaga", "text")]


def _orders_editor_frame(plan: dict) -> pd.DataFrame:
    rows = []
    for o in plan["orders"]:
        r = {}
        for f, label, kind in ORDER_EDIT_COLUMNS:
            v = o.get(f)
            r[label] = (date.fromisoformat(v) if v else None) if kind == "date" else v
        rows.append(r)
    cols = {label: (pd.Series(dtype="object") if kind == "date" else pd.Series(dtype="float" if kind == "num" else "str"))
            for _, label, kind in ORDER_EDIT_COLUMNS}
    return pd.DataFrame(rows, columns=list(cols)) if rows else pd.DataFrame(cols)


def _rows_from_editor(plan: dict, edited: pd.DataFrame) -> list[dict]:
    old = {o["id"]: o for o in plan["orders"]}
    used = {str(x).strip() for x in edited["Nr"] if isinstance(x, str) and x.strip()}
    rows, n = [], 1
    for rec in edited.to_dict("records"):
        if all(pd.isna(rec[l]) or rec[l] == "" for _, l, _ in ORDER_EDIT_COLUMNS):
            continue
        oid = rec["Nr"] if isinstance(rec["Nr"], str) and rec["Nr"].strip() else None
        if oid is None:                                 # nowy wiersz bez numeru: nadajemy własny
            while f"M{n}" in used or f"M{n}" in old:
                n += 1
            oid = f"M{n}"
            used.add(oid)
        base = dict(old.get(oid.strip(), {}))
        base["id"] = oid.strip()
        for f, label, kind in ORDER_EDIT_COLUMNS:
            if f != "id":
                base[f] = None if pd.isna(rec[label]) else rec[label]
        rows.append(base)
    return rows


def _section_orders(pc: PlanCtx) -> None:
    ctx, plan = pc.ctx, pc.plan
    st.caption("Popraw dane zleceń w tabeli (kody pocztowe, daty, LDM, wagę, cenę) - zmiany zapisują się i przeliczają koszty "
               "od razu. Nowy wiersz (numer zostaw pusty, dostanie M1, M2…) zapisze się, gdy uzupełnisz kraj i kod "
               "załadunku oraz rozładunku. Wiersz usuniesz zaznaczeniem i klawiszem Delete.")
    key = f"ws_orders_{pc.kid}_{pc.nonce}"
    edited = st.data_editor(
        _orders_editor_frame(plan), key=key, num_rows="dynamic", hide_index=True, width="stretch",
        column_config={
            "Nr": st.column_config.TextColumn("Nr", width=80),
            "Zleceniodawca": st.column_config.TextColumn("Zleceniodawca", width=130),
            "Zał. kraj": st.column_config.TextColumn("Zał. kraj", max_chars=2, width=75),
            "Zał. kod": st.column_config.TextColumn("Zał. kod", width=90),
            "Zał. miasto": st.column_config.TextColumn("Zał. miasto", width=120),
            "Data zał.": st.column_config.DateColumn("Data zał.", format="DD.MM.YYYY", width=115),
            "Rozł. kraj": st.column_config.TextColumn("Rozł. kraj", max_chars=2, width=80),
            "Rozł. kod": st.column_config.TextColumn("Rozł. kod", width=90),
            "Rozł. miasto": st.column_config.TextColumn("Rozł. miasto", width=120),
            "Data rozł.": st.column_config.DateColumn("Data rozł.", format="DD.MM.YYYY", width=115),
            "LDM": st.column_config.NumberColumn("LDM", min_value=0.0, max_value=13.6, format="%.1f", width=70),
            "Waga (t)": st.column_config.NumberColumn("Waga (t)", min_value=0.0, max_value=24.0, format="%.1f", width=80),
            "Cena (EUR)": st.column_config.NumberColumn("Cena (EUR)", min_value=0.0, format="%.0f", width=95),
            "Uwaga": st.column_config.TextColumn("Uwaga", width=160)})
    rows = _rows_from_editor(plan, edited)
    if ws.orders_differ(plan, rows):                    # każda zmiana zapisuje się i przelicza koszty od razu
        def mutate(p):
            res = ws.update_orders(p, [r for r in rows if r["id"] in {x["id"] for x in p["orders"]}
                                       or all(r.get(f) for f in ws.LOCATION_FIELDS)], geocode=ctx.geocode)
            parts = []
            if res["added"]:
                parts.append(f"dodano {', '.join(res['added'])}")
            if res["removed"]:
                parts.append(f"usunięto {', '.join(res['removed'])}")
            if res["relocated"]:
                parts.append(f"nowe położenie: {', '.join(res['relocated'])}")
            if parts:
                _flash("success", "Zapisano zlecenia (" + "; ".join(parts) + ").")
        pc.apply(mutate)


# --- sekcja: kolejność punktów
def _stop_place(pc: PlanCtx, s: dict) -> str:
    if s["kind"] == "W":
        return f"{s['country']} {s['postal']} {s['city']}".strip()
    o = next((x for x in pc.plan["orders"] if x["id"] == s["order_id"]), None)
    if not o:
        return "?"
    side = "start" if s["kind"] == "L" else "end"
    return f"{o[f'{side}_country']} {o[f'{side}_postal']} {o[f'{side}_city']}".strip()


def _section_stops(pc: PlanCtx) -> None:
    plan = pc.plan
    if not plan["stops"]:
        empty_state("🧭", "Plan nie ma jeszcze punktów. Dodaj zlecenia w sekcji „➕ Dodaj”.")
        return
    st.caption("Zmień numer w kolumnie „Nr”, aby przestawić punkt (mniejszy numer = wcześniej). Zaznacz „Usuń”, żeby wyrzucić "
               "punkt dodatkowy albo całe zlecenie (usunięcie załadunku lub rozładunku usuwa oba punkty zlecenia). "
               "Zmiany zapisują się i przeliczają koszty od razu.")
    kinds = {"L": "🟢▲ Załadunek", "U": "🔴▼ Rozładunek", "W": "📍 Punkt dodatkowy"}
    df = pd.DataFrame({
        "Nr": list(range(1, len(plan["stops"]) + 1)),
        "Typ": [kinds[s["kind"]] for s in plan["stops"]],
        "Zlecenie / punkt": [s["order_id"] for s in plan["stops"]],
        "Miejsce": [_stop_place(pc, s) for s in plan["stops"]],
        "Uwaga": [s.get("note", "") if s["kind"] == "W" else "" for s in plan["stops"]],
        "Usuń": [False] * len(plan["stops"])})
    key = f"ws_stops_{pc.kid}_{pc.nonce}"
    edited = st.data_editor(df, key=key, hide_index=True, width="stretch", num_rows="fixed",
                            disabled=["Typ", "Zlecenie / punkt", "Miejsce"],
                            column_config={"Nr": st.column_config.NumberColumn("Nr", min_value=1, step=1, width=70),
                                           "Typ": st.column_config.TextColumn("Typ", width=160),
                                           "Zlecenie / punkt": st.column_config.TextColumn("Zlecenie / punkt", width=130),
                                           "Miejsce": st.column_config.TextColumn("Miejsce", width=240),
                                           "Uwaga": st.column_config.TextColumn("Uwaga (punkty dodatkowe)", width=200),
                                           "Usuń": st.column_config.CheckboxColumn("Usuń", width=70)})
    nrs = [float(x) if pd.notna(x) else float(i + 1) for i, x in enumerate(edited["Nr"])]
    dels = [bool(x) for x in edited["Usuń"]]
    notes = list(edited["Uwaga"])
    if ws.stop_edits_pending(plan, nrs, dels, notes):   # zmiana kolejności lub usunięcie zapisuje się od razu
        def mutate(p):
            dropped = ws.apply_stop_edits(p, nrs, dels, notes)
            if dropped:
                _flash("success", f"Usunięto zlecenia: {', '.join(sorted(dropped))}.")
        pc.apply(mutate)


# --- sekcja: dodawanie zleceń i punktów
def _pool_label(r) -> str:
    return (f"{r.id} · {r.client or '—'} · {r.start_country} {r.start_postal} → {r.end_country} {r.end_postal}"
            f" · {r.ldm:g} LDM · {r.price_eur:,.0f} EUR".replace(",", " "))


def _section_add(pc: PlanCtx) -> None:
    ctx, plan = pc.ctx, pc.plan
    st.markdown("**Zlecenia z bieżącego wyszukiwania**")
    pool = ctx.pool
    if pool is None or pool.empty:
        st.caption("Brak zleceń z wyszukiwania. Wczytaj oferty w zakładce „1 · Dane”, albo dodaj zlecenie ręcznie "
                   "(sekcja „🧾 Zlecenia”, pusty wiersz na dole tabeli).")
    else:
        have = set(ws.order_ids(plan))
        avail = pool[~pool["id"].isin(have)]
        q = st.text_input("Szukaj (nr, zleceniodawca, kod, miasto)", key=f"ws_pool_q_{pc.kid}").strip().lower()
        if q:
            hay = (avail["id"].astype(str) + " " + avail["client"].fillna("") + " " + avail["start_postal"].astype(str) + " "
                   + avail["end_postal"].astype(str) + " " + avail["start_city"].fillna("") + " " + avail["end_city"].fillna("")).str.lower()
            avail = avail[hay.str.contains(q, regex=False)]
        options = {r.id: _pool_label(r) for r in avail.head(400).itertuples()}
        picked = st.multiselect(f"Zlecenia do dodania ({len(avail)} dostępnych)", list(options), format_func=options.get,
                                key=f"ws_pool_pick_{pc.kid}_{pc.nonce}")
        if st.button("➕ Dodaj wybrane na koniec trasy", disabled=not picked, key=f"ws_pool_add_{pc.kid}",
                     type="primary"):
            rows = [r for r in pool.to_dict("records") if r["id"] in set(picked)]

            def mutate(p):
                added = ws.add_orders(p, [{**r, "price_eur": r.get("price_used_eur", r.get("price_eur")),
                                           "dispatcher": pc.user if pc.user != "—" else ""} for r in rows])
                _flash("success", f"Dodano zlecenia: {', '.join(added) or 'brak (już są w planie)'}. Ustaw ich kolejność w sekcji „🧭 Kolejność punktów”.")
            pc.apply(mutate)
    st.divider()
    st.markdown("**Punkt dodatkowy** (miejsce do zahaczenia po drodze: magazyn, warsztat, odbiór)")
    with st.form(f"ws_wp_{pc.kid}", clear_on_submit=True):
        c1, c2, c3 = st.columns([1, 2, 3])
        country = c1.text_input("Kraj", "PL", max_chars=2)
        postal = c2.text_input("Kod pocztowy")
        city = c3.text_input("Miejscowość / opis")
        d1, d2 = st.columns([2, 3])
        positions = ["Na końcu trasy"] + [f"Przed krokiem {i + 1}" for i in range(len(plan["stops"]))]
        where = d1.selectbox("Miejsce w trasie", positions)
        note = d2.text_input("Uwaga (opcjonalnie)")
        submitted = st.form_submit_button("➕ Dodaj punkt dodatkowy")
    if submitted:
        if len(country.strip()) != 2 or not postal.strip():
            st.warning("Podaj dwuliterowy kod kraju i kod pocztowy.")
        else:
            lat, lon = ctx.geocode(country.strip().upper(), postal.strip())
            pos = None if where == positions[0] else positions.index(where) - 1

            def mutate(p):
                wid = ws.add_waypoint(p, country, postal, city, lat, lon, note, position=pos)
                _flash("success", f"Dodano punkt {wid}: {country.upper()} {postal} {city}.")
            pc.apply(mutate)


# --- sekcja: ustawienia planu, kopiowanie, czyszczenie
def _section_settings(pc: PlanCtx) -> None:
    ctx, plan = pc.ctx, pc.plan
    with st.form(f"ws_settings_{pc.kid}"):
        name = st.text_input("Nazwa wariantu", pc.row["label"], max_chars=60, placeholder="np. Francja - czwartek")
        note = st.text_area("Notatka", plan.get("note", ""), height=90)
        manual = st.checkbox("Podaj datę i godzinę wyjazdu", value=bool(plan.get("start_at")),
                             help="Bez tego pojazd rusza automatycznie, tak żeby zdążyć na pierwsze okno załadunku.")
        cur = datetime.fromisoformat(plan["start_at"]) if plan.get("start_at") else datetime.combine(date.today(), dtime(8, 0))
        s1, s2 = st.columns(2)
        sd = s1.date_input("Data wyjazdu", cur.date(), format="DD.MM.YYYY")
        stm = s2.time_input("Godzina", cur.time(), step=900)
        saved = st.form_submit_button("💾 Zapisz ustawienia wariantu", type="primary")
    if saved:
        ctx.store.rename_variant(pc.vid, name.strip() or pc.row["label"])

        def mutate(p):
            p["name"], p["note"] = name.strip(), note
            p["start_at"] = datetime.combine(sd, stm).isoformat(timespec="minutes") if manual else None
            _flash("success", "Zapisano ustawienia wariantu.")
        pc.apply(mutate)
    st.divider()
    other_scope = "group" if pc.scope == "personal" else "personal"
    other_label = "pracy grupowej" if other_scope == "group" else "pracy osobistej"
    st.markdown(f"**Kopiuj ten wariant do {other_label}** (jako nowa trasa tego samego auta)")
    if other_scope == "personal" and pc.user == "—":
        st.caption("Wpisz swoje imię, aby kopiować do pracy osobistej.")
    else:
        other_owner = "" if other_scope == "group" else owner_key(pc.user)
        if st.button(f"📋 Kopiuj do {other_label}", key=f"ws_copy_{pc.kid}", disabled=not plan["orders"]):
            _rid, _vid = ctx.store.create_route(other_scope, other_owner, pc.vehicle["id"], plan, by=pc.user)
            _flash("success", f"Skopiowano do {other_label} jako nowa trasa {ctx.store.list_routes(other_scope, other_owner, pc.vehicle['id'])[-1]['code']}.")
            st.rerun()
    st.divider()
    st.markdown("**Wyczyść wariant**")
    sure = st.checkbox("Tak, usuń wszystkie zlecenia i punkty tego wariantu", key=f"ws_clear_ok_{pc.kid}")
    if st.button("🗑️ Wyczyść wariant", disabled=not sure, key=f"ws_clear_{pc.kid}"):
        def mutate(p):
            p.update(ws.new_plan(p.get("name", "")))
            p["start_at"] = None
            _flash("success", "Wariant wyczyszczony.")
        pc.apply(mutate)


# ------------------------------------------------------------------ wysyłanie wariantu z wyszukiwarki
def send_variant_ui(ctx: WsContext, variant, orders: pd.DataFrame, key: str) -> None:
    """Sekcja w zakładce „Trasa”: wyślij wybrany wariant do workspace - jako nową trasę auta albo jako nowy wariant istniejącej."""
    init_identity()
    vehicles = ctx.store.vehicles()
    if vehicles.empty:
        st.caption("Aby wysłać wariant do workspace, dodaj pojazdy w zakładce „🚛 Pojazdy”.")
        return
    user = user_name()
    modes = {k: v for k, v in MODES.items() if v == "group" or user}
    c1, c2 = st.columns([2, 3])
    label = c1.radio("Gdzie zapisać", list(modes), key=f"send_mode_{key}", horizontal=True,
                     help="Praca osobista wymaga imienia (zakładka Workspace)." if not user else None)
    scope = modes[label]
    owner = owner_key(user) if scope == "personal" else ""
    overview = ctx.store.routes_overview(scope, owner)
    ids = list(vehicles["id"])
    names = {v.id: _vehicle_label(v, overview) for v in vehicles.itertuples()}
    vid = c2.selectbox("Auto", ids, key=f"send_vehicle_{key}", format_func=names.get)
    routes = ctx.store.list_routes(scope, owner, vid)
    target = {0: "➕ Nowa trasa"}
    for r in routes:
        target[r["id"]] = f"Nowy wariant w trasie {r['code']}"
    choice = st.selectbox("Dodaj jako", list(target), key=f"send_target_{key}_{scope}_{vid}", format_func=target.get)

    def do_send():
        plan = ws.from_variant(variant, orders, ws.new_plan(f"Wariant {' + '.join(variant.order_ids)}"))
        for o in plan["orders"]:
            o["dispatcher"] = o["dispatcher"] or user
        try:
            if choice == 0:
                rid, var = ctx.store.create_route(scope, owner, vid, plan, by=user or "—")
            else:
                rid = choice
                var = ctx.store.add_variant(rid, plan, label="", by=user or "—")
        except Exception:                                     # trasa mogła zostać usunięta przez kogoś innego
            _flash("warning", "Nie udało się zapisać (trasa mogła zostać zmieniona) - spróbuj ponownie.")
            return
        st.session_state["ws_mode"] = label
        st.session_state["ws_vehicle"] = vid
        st.session_state[f"ws_route_{scope}_{owner}_{vid}"] = rid
        st.session_state[f"ws_variant_{rid}"] = var
        st.session_state["ws_section"] = S_TABLE
        _flash("success", f"Wysłano wariant do auta {names[vid].split(' · ')[0]}.")
        go_to(WORK_TAB_INDEX)

    st.button("🧰 Wyślij do workspace", key=f"send_btn_{key}", on_click=do_send, type="primary")
