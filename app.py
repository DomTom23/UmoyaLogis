"""Optymalizator Tras LTL - aplikacja Streamlit.  Uruchomienie: streamlit run app.py"""
from __future__ import annotations

import os
import time
from collections import Counter

import pandas as pd
import streamlit as st

from ltl.auth import password_ok
from ltl.costs import CostParams, profit
from ltl.eligibility import ALL_BODIES, DEFAULT_BODIES, FleetProfile, check_eligibility
from ltl.geo import ROAD_FACTOR, Geocoder, add_coordinates, haversine_km
from ltl.ingest import UnreadableFile, clean_edited, parse_sources_report, read_uploaded
from ltl.mapview import route_map
from ltl.optimizer import OptimizerParams, Stop, find_best_combinations
from ltl.ownpoints import build_extra_points
from ltl.ordertable import code_match, filter_orders, merge_edits, parse_codes
from ltl.pricing import DEFAULT_RULES, RULE_COLUMNS, DIRECTION_LABELS, apply_price_rules, frame_to_rules, rules_to_frame
from ltl.ranking import Ranked, rank_by_real_routes, ranked_to_frame, variant_key
from ltl.route_edit import apply_edit, evaluate_route, route_for_variant
from ltl.routing import VALHALLA_URL, RouteOptions, RouteResult, TruckProfile
from ltl.sample import SAMPLE_TEXT
from ltl.variant_detail import order_table, route_chain_html, stop_table
from ltl.storage import OUTCOMES, InvalidName, NameExists, Store

st.set_page_config(page_title="Optymalizator Tras LTL", page_icon="🚛", layout="wide")

# opcjonalne wspólne hasło (serwer biurowy): ustaw APP_PASSWORD w pliku .env
_APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
if _APP_PASSWORD and not st.session_state.get("authed"):
    st.title("🚛 Optymalizator Tras LTL")
    with st.form("login_form"):
        entered = st.text_input("Hasło", type="password")
        if st.form_submit_button("Zaloguj", type="primary"):
            if password_ok(entered, _APP_PASSWORD):
                st.session_state.authed = True
                st.rerun()
            st.error("Nieprawidłowe hasło.")
    st.stop()

st.markdown("""
<style>
  .block-container { padding-top: 2rem; max-width: 1400px; }
  .step { display: flex; align-items: center; gap: .6rem; margin: .2rem 0 .4rem 0; font-size: 1.35rem; font-weight: 650; }
  .step .n { background: #1f6feb; color: #fff; width: 1.9rem; height: 1.9rem; border-radius: 50%;
             display: inline-flex; align-items: center; justify-content: center; font-size: 1rem; }
  .hint { opacity: .75; font-size: .92rem; margin-bottom: .6rem; }
  .kpis { display: flex; flex-wrap: wrap; gap: .6rem 1rem; margin: .4rem 0 .6rem 0; }
  .kpi { flex: 1 1 125px; min-width: 110px; }
  .kpi .l { font-size: .85rem; opacity: .7; }
  .kpi .v { font-size: 1.45rem; font-weight: 600; line-height: 1.25; white-space: nowrap; }
  .kpi .d { font-size: .8rem; opacity: .8; }
  .kpi .d.pos { color: #1a9850; } .kpi .d.neg { color: #d73027; }
  .chain { margin: .2rem 0 .6rem 0; line-height: 1.9; }
  .anchor { scroll-margin-top: 4rem; }
  .nav { display: flex; flex-wrap: wrap; gap: .4rem .6rem; align-items: center; margin: .1rem 0 .8rem 0; font-size: .92rem; }
  .nav a { text-decoration: none; padding: .15rem .65rem; border-radius: 999px; border: 1px solid rgba(128,128,128,.35); color: inherit; }
  .nav a:hover { background: rgba(31,111,235,.12); border-color: #1f6feb; }
  .nav .sum { opacity: .75; margin-left: .4rem; }
  .best { border-left: 5px solid #1a9850; padding: .5rem .9rem; background: rgba(26,152,80,.08);
          border-radius: 6px; margin-bottom: .8rem; }
</style>
""", unsafe_allow_html=True)


def step(n, title: str, hint: str = "") -> None:
    st.markdown(f"<div id='krok-{n}' class='anchor'></div><div class='step'><span class='n'>{n}</span>{title}</div>",
                unsafe_allow_html=True)
    if hint:
        st.markdown(f"<div class='hint'>{hint}</div>", unsafe_allow_html=True)


def plural(n: int) -> str:
    return "zlecenie" if n == 1 else "zlecenia" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else "zleceń"


def kpis(items: list[tuple]) -> None:
    """Wskaźniki w układzie, który zawija się na wąskich ekranach (st.metric ucina wartości w wąskich kolumnach).
    items: (etykieta, wartość, podpis | None, 'pos' | 'neg' | '', podpowiedź)."""
    parts = []
    for label, value, delta, tone, help_ in items:
        delta_html = "<div class='d %s'>%s</div>" % (tone, delta) if delta else ""
        parts.append("<div class='kpi' title='%s'><div class='l'>%s</div><div class='v'>%s</div>%s</div>"
                     % (help_, label, value, delta_html))
    html = "".join(parts)
    st.markdown(f"<div class='kpis'>{html}</div>", unsafe_allow_html=True)


def eur(x: float) -> str:
    return f"{x:,.0f}".replace(",", " ") + " EUR"


# ------------------------------------------------------------ zasoby / cache
@st.cache_resource
def _geocoder(online: bool) -> Geocoder:
    return Geocoder(online=online)


@st.cache_resource
def _route_store() -> dict:
    """Pamięć podręczna tras (zapisywane są trasy, w których choć część odcinków pochodzi z routingu)."""
    return {}


@st.cache_resource
def _store() -> Store:
    return Store()


@st.cache_data(show_spinner="Rozpoznawanie zleceń…")
def _parse(named: tuple, drop_no_price: bool, pln_per_eur: float):
    # format każdego źródła wykrywany automatycznie; zwraca (tabela, raport źródeł)
    return parse_sources_report(list(named), drop_without_price=drop_no_price, pln_per_eur=pln_per_eur)


@st.cache_data(show_spinner="Szukanie najlepszych doładunków…")
def _search(orders: pd.DataFrame, cost: CostParams, opt: OptimizerParams, origin: Stop | None = None,
            waypoints: tuple = (), forced_ids: tuple = ()):
    return find_best_combinations(orders, cost, opt, origin, waypoints, forced_ids)


_variant_key = variant_key
ROUTE_STORE = _route_store()   # pobrane w głównym wątku - używane też z wątków rankingu


def get_route(v, vias) -> RouteResult:
    """Trasa po drogach; udane wyniki pamiętane, niepowodzenia tylko przez 2 minuty (potem ponowna próba)."""
    sig = (_variant_key(v), tuple((x.leg, round(x.lat, 5), round(x.lon, 5)) for x in vias),
           provider, truck, valhalla_url, ors_key, route_opts)
    hit = ROUTE_STORE.get(sig)
    if hit and (hit[1].has_real or time.time() - hit[0] < 120):
        return hit[1]
    res = route_for_variant(v, vias, truck, provider, ors_key, valhalla_url, speed, route_opts)
    ROUTE_STORE[sig] = (time.time(), res)
    return res


# ---------------------------------------------------------------- ustawienia
with st.sidebar:
    st.title("⚙️ Ustawienia")
    st.caption("Zmiany działają od razu - nie trzeba ponownie wczytywać danych.")

    with st.expander("💶 Koszty", expanded=True):
        fuel_price = st.number_input("Cena paliwa (EUR/l)", 0.5, 4.0, 1.55, 0.01)
        consumption = st.number_input("Spalanie (l/100 km)", 10.0, 60.0, 30.0, 0.5)
        toll = st.number_input("Średnie myto (EUR/km)", 0.0, 1.0, 0.20, 0.01)
        day_cost = st.number_input("Koszt dnia pracy (EUR)", 0.0, 1000.0, 300.0, 10.0,
                                   help="Auto + kierowca za jeden dzień pracy.")
        speed = st.number_input("Średnia prędkość (km/h)", 40.0, 90.0, 65.0, 1.0,
                                help="Służy do szacowania czasu przejazdu i liczby dni pracy (limit 9 h jazdy dziennie).")
        pln_per_eur = st.number_input("Kurs PLN za 1 EUR", 3.0, 6.0, 4.25, 0.05,
                                      help="Do przeliczania cen podanych w PLN. Zmiana wczytuje oferty od nowa "
                                           "(poprawki ręczne w tabeli zostają tylko do czasu zmiany kursu).")
        waypoint_h = st.number_input("Postój w punkcie dodatkowym (h)", 0.0, 24.0, 0.0, 0.5,
                                     help="Czas pracy doliczany za każdy obowiązkowy punkt dodatkowy na trasie.")
        handling_h = st.number_input("Czas na załadunek / rozładunek (h)", 0.0, 8.0, 2.0, 0.5,
                                     help="Liczony dla każdego załadunku i każdego rozładunku; wlicza się do czasu "
                                          "pracy i liczby dni (koszt dnia pracy).")
        channel_fee = st.number_input("Przeprawa przez La Manche (EUR)", 0.0, 2000.0, 0.0, 10.0,
                                      help="Eurotunel lub prom, za jedną przeprawę. Wpisz własną stawkę - "
                                           "0 oznacza, że opłata nie jest doliczana (trasa i tak jest oznaczona).")
        tunnel_fee = st.number_input("Płatny tunel alpejski (EUR)", 0.0, 1000.0, 0.0, 10.0,
                                     help="Za jeden tunel (Mont Blanc, Fréjus, Tauern…). 0 = nie doliczaj.")

    with st.expander("🔎 Szukanie doładunków", expanded=True):
        max_detour = st.slider("Maks. odchylenie trasy (km)", 0, 1000, 300, 10,
                               help="O ile km trasa połączonych zleceń może być dłuższa od najdłuższego z nich.")
        date_gap = st.number_input("Maks. różnica dat załadunku (dni)", 0, 14, 3,
                                   help="0 = bez limitu.")
        max_orders = st.slider("Maks. zleceń w wariancie", 2, 12, 6,
                               help="Górny limit liczby zleceń na jednej naczepie (poza limitem 13,6 LDM / 24 t).")
        top_n = st.slider("Ile wariantów pokazać", 3, 20, 10)
        n_check = st.slider("Ile kandydatów sprawdzić na drogach", 10, 40, 20,
                            help="Najlepsze warianty wg szacunku są liczone po rzeczywistych trasach, a ranking "
                                 "układany od nowa. Więcej = dokładniej, ale dłużej (publiczny serwer tras).")

    with st.expander("🚚 Dopasowanie ofert do zestawu"):
        st.caption("Oferty niespełniające warunków nie trafiają do wariantów (powód widać w tabeli).")
        fleet = FleetProfile(
            semi_trailer_only=st.checkbox("Tylko oferty dopuszczające naczepę", True,
                                          help="Oferty tylko dla busa lub „solo” (bez „z naczepą”) odpadają."),
            accepted_bodies=tuple(st.multiselect("Nadwozia naszego zestawu", list(ALL_BODIES), list(DEFAULT_BODIES),
                                                 help="Oferta musi dopuszczać któreś z nich (brak informacji = pasuje).")),
            adr=st.checkbox("Mamy uprawnienia ADR", False),
            tail_lift=st.checkbox("Mamy windę załadowczą", False),
            exclude_cabotage=st.checkbox("Wyklucz kabotaż (odbiór i dowóz w tym samym kraju)", True,
                                         help="Oferty, w których załadunek i rozładunek są w jednym kraju "
                                              "(np. DE → DE), nie trafiają do wariantów."),
            allow_ftl=st.checkbox("Łącz też oferty FTL", True,
                                  help="FTL zwykle oznacza cały pojazd dla jednego zleceniodawcy. "
                                       "Odznacz, aby do doładunków brać tylko oferty LTL."),
        )

    with st.expander("💱 Ceny szacunkowe (oferty bez ceny)"):
        use_est = st.checkbox("Używaj cen szacunkowych", value=True,
                              help="Oferta bez ceny dostaje cenę = stawka × LDM wg reguły dla kraju załadunku "
                                   "lub rozładunku. Trafia do wariantów tylko, gdy ta cena osiąga minimum.")
        st.caption("Stawka EUR/LDM i minimalny przychód dla kraju załadunku („z kraju”) lub rozładunku "
                   "(„do kraju”) albo któregokolwiek z nich („z lub do kraju”); w polu Kraj można podać kilka kodów po przecinku. "
                   "Gdy pasuje kilka reguł, obowiązuje najwyższa stawka. Oferty krajowe (kabotaż) są pomijane.")
        rules_df = st.data_editor(
            rules_to_frame(DEFAULT_RULES), key="rules_editor", num_rows="dynamic", hide_index=True, width="stretch",
            column_config={
                "Aktywna": st.column_config.CheckboxColumn("Akt.", width="small"),
                "Kierunek": st.column_config.SelectboxColumn("Kierunek", options=list(DIRECTION_LABELS.values())),
                "Kraj": st.column_config.TextColumn("Kraj", max_chars=40, width="small", help="Kod kraju (np. CH) albo kilka kodów po przecinku (BE,NL,LU,DE)."),
                "EUR/LDM": st.column_config.NumberColumn("EUR/LDM", min_value=0.0, format="%.0f"),
                "Min. przychód (EUR)": st.column_config.NumberColumn("Min. (EUR)", min_value=0.0, format="%.0f"),
            })
        price_rules = frame_to_rules(rules_df)

    with st.expander("🛣️ Trasy po drogach"):
        avoid_ch = st.checkbox("Omijaj Szwajcarię", value=True,
                               help="Trasa nie przejedzie przez Szwajcarię; jeśli serwer jej nie wyłącza, "
                                    "aplikacja szuka objazdu (przez Francję / Włochy / Austrię / Niemcy).")
        provider_label = st.selectbox("Silnik tras", [
            "Valhalla - ciężarówka (bez klucza)", "OpenRouteService - HGV (klucz API)", "Brak (linie proste)"])
        provider = {"V": "valhalla", "O": "ors"}.get(provider_label[0], "none")
        valhalla_url = st.text_input("Adres serwera Valhalla", os.environ.get("VALHALLA_URL", VALHALLA_URL),
                                     help="Publiczny serwer ma limit 1500 km na zapytanie; własny nie.") \
            if provider == "valhalla" else VALHALLA_URL
        ors_key = st.text_input("Klucz API OpenRouteService", os.environ.get("ORS_API_KEY", ""), type="password") \
            if provider == "ors" else ""
        st.caption("Parametry zestawu (drogi z ograniczeniami):")
        truck = TruckProfile(
            gross_weight_t=st.number_input("DMC zestawu (t)", 3.5, 60.0, 40.0, 0.5),
            axle_load_t=st.number_input("Nacisk osi (t)", 3.0, 13.0, 11.5, 0.5),
            height_m=st.number_input("Wysokość (m)", 2.0, 4.5, 4.0, 0.05),
            width_m=st.number_input("Szerokość (m)", 2.0, 2.6, 2.55, 0.05),
            length_m=st.number_input("Długość (m)", 6.0, 18.75, 16.5, 0.05),
            hazmat=st.checkbox("ADR / materiały niebezpieczne", False),
        )

cost = CostParams(day_cost_eur=day_cost, avg_speed_kmh=speed, fuel_l_per_100km=consumption,
                  fuel_price_eur=fuel_price, toll_eur_per_km=toll,
                  channel_crossing_eur=channel_fee, alpine_tunnel_eur=tunnel_fee, handling_h_per_stop=handling_h,
                  waypoint_stop_h=waypoint_h)
route_opts = RouteOptions(avoid_switzerland=avoid_ch)
opt = OptimizerParams(max_detour_km=max_detour, top_n=max(n_check, top_n),   # kandydaci do sprawdzenia na drogach
                      hard_cap_orders=max_orders, max_load_date_gap_days=int(date_gap) or None)

# ------------------------------------------------------------------ nagłówek
st.title("🚛 Optymalizator Tras LTL")
st.caption("Wklej oferty z giełdy, a aplikacja znajdzie najbardziej opłacalne doładunki "
           "(naczepa 13,6 LDM / 24 t, trasa kończy się na ostatnim rozładunku).")

nav_slot = st.empty()          # pasek skrótów do kroków; wypełniany na końcu skryptu (znane są już wyniki)

# ------------------------------------------------------------ krok 1: dane
st.session_state.setdefault("uploader_n", 0)
st.session_state.setdefault("raw_text", "")


def _load_example() -> None:
    st.session_state.raw_text = SAMPLE_TEXT


def _clear_all() -> None:
    for k in ("analysis", "raw_text", "vias", "ranked", "seen_nonce", "loaded_name", "save_name"):
        st.session_state.pop(k, None)
    st.session_state.uploader_n += 1


_has_data = bool(st.session_state.get("analysis"))
with (st.expander("1 · Dane z giełdy - zmień lub dodaj oferty", expanded=False) if _has_data
      else st.container(border=True)):
    if not _has_data:
        step(1, "Dane z giełdy", "Wklej tekst z Timocom lub Trans.eu i/lub dodaj pliki. Format jest rozpoznawany automatycznie.")
    with st.form("input_form", border=False):
        left, right = st.columns([3, 2])
        raw_text = left.text_area("Wklejone oferty", key="raw_text", height=190,
                                  placeholder="Wklej tutaj skopiowane oferty (Ctrl+V)…")
        files = right.file_uploader("Pliki (txt, csv, tsv, xlsx)", type=["txt", "csv", "tsv", "xlsx", "xls"],
                                    accept_multiple_files=True, key=f"files_{st.session_state.uploader_n}",
                                    help="Wklejony tekst i pliki są łączone; każde źródło może mieć inny format.")
        o1, o2 = st.columns(2)
        online = o1.checkbox("Ustal położenie kodów pocztowych przez internet", value=True, key="opt_online",
                             help="zippopotam.us i Nominatim. Bez tego używane są środki krajów (mało dokładne).")
        drop_no_price = o2.checkbox("Pomiń oferty bez ceny", value=False, key="opt_drop")
        keep_history = st.checkbox("Zapisz oferty w historii", value=True, key="opt_history",
                                   help="Każdy import trafia do historii ofert (bez duplikatów); w historii można "
                                        "szukać po zleceniodawcy i wpisywać ceny zrealizowane.")
        submitted = st.form_submit_button("🔍 Szukaj doładunków", type="primary", width="stretch")
    b1, b2, _ = st.columns([1, 1, 4])
    b1.button("📋 Wczytaj przykład", on_click=_load_example, width="stretch")
    b2.button("🗑️ Wyczyść", on_click=_clear_all, width="stretch")

def _load_saved(name: str) -> None:
    try:
        df = _store().load_orders(name)
    except KeyError:
        st.toast(f"Nie znaleziono tabeli „{name}”.", icon="⚠️")
        return
    prev = st.session_state.get("analysis", {}).get("id", 0)
    st.session_state.analysis = {"id": prev + 1, "df": df, "texts": (), "drop": False,
                                 "online": st.session_state.get("opt_online", True)}
    st.session_state.pop("ranked", None)
    st.session_state.loaded_name = st.session_state.save_name = name
    st.toast(f"Wczytano tabelę „{name}” ({len(df)} zleceń).", icon="📂")


def _delete_saved(name: str) -> None:
    if _store().delete("orders", name):
        st.toast(f"Usunięto tabelę „{name}”.", icon="🗑️")


with st.expander(f"📂 Zapisane tabele zleceń ({len(_store().list_tables('orders'))})"):
    saved = _store().list_tables("orders")
    if saved.empty:
        st.caption("Nic jeszcze nie zapisano. Po wczytaniu danych użyj „Zapisz tabelę” w kroku 2.")
    else:
        info = {r.name: f"{r.name} · {r.n_rows} zleceń · {r.updated_at.replace('T', ' ')}" for r in saved.itertuples()}
        choice = st.selectbox("Wybierz zapisaną tabelę", list(info), format_func=info.get, key="saved_choice")
        c_load, c_del, c_ok = st.columns([1, 1, 2])
        c_load.button("📂 Wczytaj", on_click=_load_saved, args=(choice,), width="stretch", key="btn_load_saved")
        confirm = c_ok.checkbox("Potwierdzam usunięcie", key="confirm_delete")
        c_del.button("🗑️ Usuń", on_click=_delete_saved, args=(choice,), width="stretch", disabled=not confirm,
                     key="btn_del_saved")

if submitted:
    st.session_state.pop("loaded_name", None)
    named, read_errors = [("Wklejony tekst", raw_text)], []
    for f in files:
        try:
            named.append((f.name, read_uploaded(f.name, f.getvalue())))
        except UnreadableFile as e:
            read_errors.append(f"Plik „{f.name}”: {e}.")
    if any(t.strip() for _, t in named):
        prev = st.session_state.get("analysis", {}).get("id", 0)
        st.session_state.analysis = {"id": prev + 1, "texts": tuple(named), "drop": drop_no_price, "online": online,
                                     "record": keep_history, "errors": read_errors}
        st.session_state.pop("ranked", None)
        st.rerun()                   # od razu zwija krok 1 (inaczej strona przeskoczyłaby przy następnym kliknięciu)
    else:
        for msg in read_errors:
            st.error(msg)
        st.warning("Wklej oferty albo dodaj plik, a potem kliknij „Szukaj doładunków”." if not files else
                   "Z dodanych plików nie udało się odczytać żadnych danych.")

analysis = st.session_state.get("analysis")
parsed = None
if analysis:
    if "df" in analysis:
        parsed, src_report = analysis["df"], []
    else:
        parsed, src_report = _parse(analysis["texts"], analysis["drop"], pln_per_eur)
    if analysis.get("record") and not analysis.get("recorded") and not parsed.empty:
        analysis["recorded"] = True       # raz na import
        rec = _store().record_offers(parsed)
        st.toast(f"Historia ofert: {rec['new']} nowych, {rec['repeated']} już znanych.", icon="📜")


def source_notes(report: list, errors: list) -> None:
    """Co wczytano z którego źródła (wklejony tekst, każdy plik): błędy, puste pliki, powtórzenia."""
    for msg in errors:
        st.error(msg)
    used = [r for r in report if not r["empty"]]
    for r in used:
        if r["found"] == 0:
            st.warning(f"„{r['name']}”: nie rozpoznano żadnych ofert. Potrzebne są kraj i kod pocztowy załadunku "
                       "oraz rozładunku każdej oferty (np. „DE 20095 Hamburg”); w arkuszu pomagają nagłówki kolumn "
                       "(Kraj zał., Kod zał., Kraj rozł., Kod rozł., Data, Waga, LDM, Cena).")
    ok = [r for r in used if r["found"]]
    if len(report) > 1 and ok and (len(ok) > 1 or any(r["name"] != "Wklejony tekst" for r in ok)):
        parts = []
        for r in ok:
            dup = r["found"] - r["kept"]
            parts.append(f"**{r['name']}**: {r['found']}" + (f" (w tym {dup} powtórzeń z innych źródeł pominięto)" if dup else ""))
        st.caption("Wczytano ofert - " + " · ".join(parts))


def render_history() -> None:
    store = _store()
    n_all = store.history_count()
    with st.expander(f"📜 Historia ofert ({n_all})"):
        if n_all == 0:
            st.caption("Historia jest pusta. Oferty zapisują się automatycznie przy każdym wyszukiwaniu "
                       "(opcja „Zapisz oferty w historii” w kroku 1).")
            return
        all_h = store.query_history(limit=50000)
        c1, c2, c3 = st.columns(3)
        h_clients = c1.multiselect("Zleceniodawca", store.history_clients(), key="h_clients", placeholder="Wszyscy")
        h_text = c2.text_input("Szukaj (zleceniodawca, miasto, kod)", key="h_text")
        h_outcome = c3.selectbox("Wynik", ["(wszystkie)"] + OUTCOMES[1:], key="h_outcome")
        d1, d2, d3, d4 = st.columns(4)
        h_from = d1.multiselect("Kraj załadunku", sorted(all_h.start_country.dropna().unique()), key="h_from", placeholder="Dowolny")
        h_to = d2.multiselect("Kraj rozładunku", sorted(all_h.end_country.dropna().unique()), key="h_to", placeholder="Dowolny")
        h_d1 = d3.date_input("Od (data zobaczenia)", value=None, key="h_d1", format="DD.MM.YYYY")
        h_d2 = d4.date_input("Do", value=None, key="h_d2", format="DD.MM.YYYY")
        e1, e2 = st.columns(2)
        h_fc = e1.text_input("Kod załadunku (początek)", key="h_fc", placeholder="np. DE 20, FR 77",
                             help="Wyszukiwanie po kodzie pocztowym (z krajem lub bez); kilka kodów rozdziel przecinkiem.")
        h_tc = e2.text_input("Kod rozładunku (początek)", key="h_tc", placeholder="np. PL 00, GB M")
        flt = dict(clients=h_clients, text=h_text, from_countries=h_from, to_countries=h_to, date_from=h_d1,
                   date_to=h_d2, outcome="" if h_outcome == "(wszystkie)" else h_outcome)
        h = filter_orders(store.query_history(limit=3000, **flt), from_codes=h_fc, to_codes=h_tc)
        priced = h[h.price_eur.notna()]
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Oferty", len(h))
        m2.metric("Mediana ceny", eur(priced.price_eur.median()) if len(priced) else "—")
        per_ldm = (priced.price_eur / priced.ldm.where(priced.ldm > 0)).dropna()
        per_km = (priced.price_eur / priced.distance_km.where(priced.distance_km > 0)).dropna()
        m3.metric("Śr. EUR / LDM", f"{per_ldm.mean():.0f}" if len(per_ldm) else "—")
        m4.metric("Śr. EUR / km", f"{per_km.mean():.2f}" if len(per_km) else "—")
        st.caption("Kolumny „Cena zrealizowana”, „Wynik” i „Notatka” można edytować - przydadzą się do analizy cen.")
        fsig = hash(tuple(map(str, [*flt.values(), h_fc, h_tc])))
        edited_h = st.data_editor(
            h, key=f"hist_editor_{fsig}", hide_index=True, width="stretch", num_rows="fixed",
            disabled=[c for c in h.columns if c not in ("realized_price_eur", "outcome", "note")],
            column_order=["first_seen_at", "client", "start_country", "start_postal", "start_city", "end_country",
                          "end_postal", "end_city", "load_date", "weight_t", "ldm", "distance_km", "price_eur",
                          "realized_price_eur", "outcome", "note", "seen_count", "last_seen_at"],
            column_config={
                "first_seen_at": st.column_config.TextColumn("Zobaczono", width="medium"),
                "last_seen_at": st.column_config.TextColumn("Ostatnio", width="medium"),
                "seen_count": st.column_config.NumberColumn("Razy", width="small"),
                "client": st.column_config.TextColumn("Zleceniodawca"),
                "start_country": st.column_config.TextColumn("Kraj zał.", width="small"),
                "start_postal": st.column_config.TextColumn("Kod zał.", width="small"),
                "start_city": st.column_config.TextColumn("Miasto zał."),
                "end_country": st.column_config.TextColumn("Kraj rozł.", width="small"),
                "end_postal": st.column_config.TextColumn("Kod rozł.", width="small"),
                "end_city": st.column_config.TextColumn("Miasto rozł."),
                "load_date": st.column_config.DateColumn("Data zał.", format="DD.MM.YYYY"),
                "weight_t": st.column_config.NumberColumn("Waga (t)", format="%.2f"),
                "ldm": st.column_config.NumberColumn("LDM", format="%.2f"),
                "distance_km": st.column_config.NumberColumn("Dystans (km)", format="%.0f"),
                "price_eur": st.column_config.NumberColumn("Cena oferty (EUR)", format="%.0f"),
                "realized_price_eur": st.column_config.NumberColumn("Cena zrealizowana (EUR)", min_value=0.0,
                                                                     format="%.0f"),
                "outcome": st.column_config.SelectboxColumn("Wynik", options=OUTCOMES),
                "note": st.column_config.TextColumn("Notatka"),
            })
        s1, s2, _ = st.columns([1, 1, 3])
        if s1.button("💾 Zapisz zmiany", key="h_save", width="stretch"):
            keys = ["id", "realized_price_eur", "outcome", "note"]
            old, new = h[keys].set_index("id"), edited_h[keys].set_index("id")
            diff = (old.fillna("") != new.fillna("")).any(axis=1)
            n_changed = _store().update_history(edited_h.loc[diff.reindex(edited_h["id"]).values, keys])
            st.toast(f"Zapisano zmiany w historii ({n_changed}).", icon="💾")
        s2.download_button("⬇️ Pobierz CSV", h.to_csv(index=False, sep=";").encode("utf-8-sig"),
                           file_name="historia_ofert.csv", mime="text/csv", width="stretch")


render_history()

if not analysis:
    with st.expander("ℹ️ Jak to działa", expanded=True):
        st.markdown(
            "1. **Wklej oferty** z giełdy (lub kliknij *Wczytaj przykład*) i naciśnij **Szukaj doładunków**.\n"
            "2. **Sprawdź zlecenia** - popraw błędy, uzupełnij brakujące ceny.\n"
            "3. **Podaj start pojazdu** (kod pocztowy) i ewentualne **punkty dodatkowe** - lub pomiń.\n"
            "4. **Wybierz wariant** z listy najlepszych doładunków (zakładka „Warianty”).\n"
            "5. **Dopracuj trasę** na mapie: przeciągnij fragment linii, a koszty przeliczą się same.")
    st.stop()

if parsed.empty:
    source_notes(src_report, analysis.get("errors", []))
    st.error("Nie rozpoznano żadnych zleceń. W danych muszą być kody w formacie „kraj kod” "
             "(np. FR 77140, GB G1 1) dla miejsca załadunku i rozładunku każdej oferty.")
    st.stop()

# --------------------------------------------------------- krok 2: zlecenia
with st.container(border=True):
    step(2, "Sprawdź zlecenia",
         f"{'Wczytano zapisaną tabelę „' + st.session_state['loaded_name'] + '”: ' if st.session_state.get('loaded_name') else 'Rozpoznano '}"
         f"{len(parsed)} ofert (źródło: {', '.join(sorted(set(parsed['source'])) or ['—'])}). "
         "Kliknij komórkę, aby poprawić wartość; wiersze można dodawać i usuwać. Zmiany od razu wchodzą do wyników.")
    source_notes(src_report, analysis.get("errors", []))
    if st.session_state.get("work_for") != analysis["id"]:       # nowa analiza: nowy zbiór roboczy
        st.session_state.work = parsed.reset_index(drop=True)
        st.session_state.work_for = analysis["id"]
        st.session_state.pop("fsig", None)
        st.session_state.pop("last_view", None)
    work = st.session_state.work

    aid = analysis["id"]
    show_details = st.toggle("Pokaż szczegóły ofert (pojazdy, nadwozie, dodatki, opis)", key=f"details_{aid}")
    f1, f2, f3 = st.columns([2, 2, 1])
    c1, c2 = st.columns(2)
    flt_from = c1.text_input("Kod załadunku (początek)", key=f"flt_from_{aid}", placeholder="np. DE 20, FR 77, PL",
                             help="Szukanie po kodzie pocztowym - wygodne, gdy z giełdy nie kopiuje się nazwa "
                                  "zleceniodawcy. Kilka kodów rozdziel przecinkiem; wystarczy początek kodu, "
                                  "z krajem (DE 20) lub bez (20095).")
    flt_to = c2.text_input("Kod rozładunku (początek)", key=f"flt_to_{aid}", placeholder="np. PL 00, GB M",
                           help="Jak obok, ale dla miejsca rozładunku.")
    sel_clients = f1.multiselect("Filtr: zleceniodawca", sorted(set(work["client"].dropna()) - {""}),
                                 key=f"flt_clients_{aid}", placeholder="Wszyscy", help="Pokazuje tylko zlecenia wybranych zleceniodawców.")
    q = f2.text_input("Szukaj (zleceniodawca, miasto, kod, nr)", key=f"flt_q_{aid}")
    only_np = f3.checkbox("Tylko bez ceny", key=f"flt_np_{aid}")
    fsig = (tuple(sel_clients), q, only_np, flt_from, flt_to)
    if st.session_state.get("fsig") != fsig:          # zmiana filtra: zatwierdź poprawki z poprzedniego widoku
        last = st.session_state.get("last_view")
        if last is not None:
            work = merge_edits(work, last["ids"], last["edited"])
            st.session_state.work = work
        st.session_state.fsig = fsig
    view = filter_orders(work, sel_clients, q, only_np, flt_from, flt_to)
    if len(view) < len(work):
        st.caption(f"Widok: {len(view)} z {len(work)} zleceń. Filtry zmieniają tylko widok tabeli - "
                   "do szukania doładunków trafiają wszystkie zlecenia.")
    edited = st.data_editor(
        view, key=f"orders_{aid}_{abs(hash(fsig))}", num_rows="dynamic", width="stretch", hide_index=True,
        disabled=["id", "source", "status", "defaults_used"],
        column_order=["id", "source", "client", "start_country", "start_postal", "start_city", "end_country",
                      "end_postal", "end_city", "load_date", "unload_date", "weight_t", "ldm", "distance_km",
                      "price_eur", "load_type", "status", "defaults_used",
                      *(["vehicles", "body", "extras", "description"] if show_details else [])],
        column_config={
            "id": st.column_config.TextColumn("Nr", width="small"),
            "source": st.column_config.TextColumn("Źródło", width="small"),
            "client": st.column_config.TextColumn("Zleceniodawca", width="medium"),
            "start_country": st.column_config.TextColumn("Kraj zał.", width="small"),
            "start_postal": st.column_config.TextColumn("Kod zał.", width="small"),
            "start_city": st.column_config.TextColumn("Miasto zał."),
            "end_country": st.column_config.TextColumn("Kraj rozł.", width="small"),
            "end_postal": st.column_config.TextColumn("Kod rozł.", width="small"),
            "end_city": st.column_config.TextColumn("Miasto rozł."),
            "load_date": st.column_config.DateColumn("Data zał.", format="DD.MM.YYYY"),
            "unload_date": st.column_config.DateColumn("Data rozł.", format="DD.MM.YYYY"),
            "weight_t": st.column_config.NumberColumn("Waga (t)", min_value=0.0, max_value=40.0, format="%.2f"),
            "ldm": st.column_config.NumberColumn("LDM", min_value=0.0, max_value=13.6, format="%.2f"),
            "distance_km": st.column_config.NumberColumn("Dystans (km)", min_value=0.0, format="%.0f"),
            "price_eur": st.column_config.NumberColumn("Cena (EUR)", min_value=0.0, format="%.0f"),
            "load_type": st.column_config.TextColumn("Typ", width="small", help="FTL = cały pojazd, LTL = ładunek częściowy."),
            "vehicles": st.column_config.TextColumn("Pojazdy"),
            "body": st.column_config.TextColumn("Nadwozie"),
            "extras": st.column_config.TextColumn("Dodatki"),
            "description": st.column_config.TextColumn("Opis oferty"),
            "status": st.column_config.TextColumn("Status", width="small"),
            "defaults_used": st.column_config.TextColumn(
                "Uwagi", help="Uzupełnione wartości domyślne, przeliczenia walut i ostrzeżenia o danych."),
        },
    )
    st.session_state.last_view = {"ids": view["id"].tolist(), "edited": edited}
    orders = clean_edited(merge_edits(work, view["id"].tolist(), edited))
    if orders.empty:
        st.warning("Brak poprawnych wierszy - potrzebne są kraj i kod pocztowy załadunku oraz rozładunku.")
        st.stop()

    with st.form("save_orders_form", border=False):
        f1, f2, f3 = st.columns([3, 2, 1])
        save_name = f1.text_input("Nazwa tabeli", key="save_name", placeholder="np. Tydzień 42 - Francja → UK",
                                  help="Zapisuje tabelę z Twoimi poprawkami; można ją później wczytać w kroku 1.")
        overwrite = f2.checkbox("Nadpisz, jeśli nazwa już istnieje", key="save_overwrite")
        save_clicked = f3.form_submit_button("💾 Zapisz tabelę", width="stretch")
    if st.session_state.get("save_flash"):
        st.success(st.session_state.pop("save_flash"))
    if save_clicked:
        try:
            saved_as = _store().save_orders(save_name, orders, overwrite=overwrite)
            st.session_state.loaded_name = saved_as
            # lista „Zapisane tabele” leży wyżej na stronie i była już narysowana: odświeżamy, żeby nowa tabela
            # była od razu widoczna (inaczej wyglądało to tak, jakby zapis się nie udał)
            st.session_state.save_flash = f"Zapisano tabelę „{saved_as}” ({len(orders)} zleceń)."
            st.rerun()
        except NameExists:
            st.warning(f"Tabela „{save_name.strip()}” już istnieje - zaznacz „Nadpisz” albo wybierz inną nazwę.")
        except InvalidName as e:
            st.warning(str(e))
    # 1) ceny (z oferty / szacunkowe) i kwalifikacja ofert dla naszego zestawu - jeszcze bez geokodowania
    orders = apply_price_rules(orders, price_rules, use_est)
    fleet_why = check_eligibility(orders, fleet)
    orders["exclusion"] = [w or ("bez ceny: " + note if kind == "brak" else "")
                           for w, kind, note in zip(fleet_why, orders["price_kind"], orders["price_note"])]
    use = orders["exclusion"] == ""

    # 2) położenie tylko dla ofert, które wejdą do wyszukiwania (duże wklejki mają setki kodów)
    geocoder = _geocoder(analysis["online"])
    subset = orders[use]
    pairs = [(c, p) for c, p in zip(subset["start_country"], subset["start_postal"])] + \
            [(c, p) for c, p in zip(subset["end_country"], subset["end_postal"])]
    if pairs:
        bar = st.progress(0.0, text="Ustalanie położenia kodów pocztowych…")
        geo_stats = geocoder.prefetch(
            pairs, progress=lambda d, t: bar.progress(min(d / max(t, 1), 1.0), text=f"Kody pocztowe: {d}/{t}"))
        bar.empty()
        located = add_coordinates(subset, geocoder)
    else:
        geo_stats, located = {"missing": 0}, subset.assign(start_lat=0.0, start_lon=0.0, end_lat=0.0, end_lon=0.0,
                                                         start_geo="", end_geo="", geo_precise=True)
    for col in ("start_lat", "start_lon", "end_lat", "end_lon", "start_geo", "end_geo", "geo_precise"):
        orders[col] = located[col].reindex(orders.index)
    orders["geo_precise"] = orders["geo_precise"].fillna(True).astype(bool)

    est = haversine_km(orders.start_lat, orders.start_lon, orders.end_lat, orders.end_lon) * ROAD_FACTOR
    orders["distance_est_km"] = est.round(0)
    dist_used = orders["distance_km"].fillna(orders["distance_est_km"])
    orders["profit_single_eur"] = [round(profit(p, d, 2, cost)["profit"], 0) if (pd.notna(p) and pd.notna(d)) else None
                                   for p, d in zip(orders["price_used_eur"], dist_used)]

    n_use = int(use.sum())
    st.markdown(f"**Do wyszukiwania weszło {n_use} z {len(orders)} ofert.**")
    if n_use < len(orders):
        reasons = Counter(r.split(":")[0] if r.startswith("bez ceny") else r
                          for why in orders.loc[~use, "exclusion"] for r in why.split(", ") if r)
        st.caption("Wykluczone (oferta może mieć kilka powodów): " +
                   "; ".join(f"{r} - {n}" for r, n in reasons.most_common()) +
                   ". Warunki zmienisz w panelu bocznym („Dopasowanie ofert do zestawu”, „Ceny szacunkowe”).")
    est_rows = orders[orders["price_kind"] == "szacunek"]
    if len(est_rows):
        st.info(f"{len(est_rows)} ofert bez ceny dostało cenę szacunkową wg reguł (panel boczny, „Ceny szacunkowe”).")
    if n_use and not orders.loc[use, "geo_precise"].all():
        n_bad = int((~orders.loc[use, "geo_precise"]).sum())
        st.warning(f"{n_bad} ofert ma kod pocztowy, którego nie udało się zlokalizować - użyto środka kraju, więc "
                   "odległości i trasy są mocno przybliżone. Sprawdź połączenie z internetem lub kody pocztowe.")
    with st.expander("Szczegóły: kwalifikacja ofert, ceny szacunkowe, dystans i zysk pojedynczych zleceń"):
        st.dataframe(
            orders[["id", "client", "start_postal", "end_postal", "price_eur", "price_used_eur", "price_kind",
                    "price_note", "exclusion", "distance_km", "distance_est_km", "profit_single_eur"]],
            width="stretch", hide_index=True,
            column_config={"id": "Nr", "client": "Zleceniodawca", "start_postal": "Kod zał.", "end_postal": "Kod rozł.",
                           "price_eur": st.column_config.NumberColumn("Cena oferty (EUR)", format="%.0f"),
                           "price_used_eur": st.column_config.NumberColumn("Cena użyta (EUR)", format="%.0f"),
                           "price_kind": "Cena z", "price_note": "Uzasadnienie ceny",
                           "exclusion": "Powód wykluczenia", "distance_km": "Dystans z oferty (km)",
                           "distance_est_km": "Dystans szac. (km)",
                           "profit_single_eur": st.column_config.NumberColumn("Zysk samodzielnie (EUR)", format="%.0f")})

orders_opt = orders[use].assign(price_eur=orders.loc[use, "price_used_eur"])    # cena z oferty lub szacunkowa
client_map = dict(zip(orders["id"], orders["client"].fillna("")))
est_ids = set(orders.loc[orders["price_kind"] == "szacunek", "id"])
price_map = dict(zip(orders["id"], orders["price_used_eur"]))
# ---------------------------------------------------- start pojazdu
with st.container(border=True):
    step(3, "Start pojazdu i punkty dodatkowe", "Wszystkie warianty liczone są od miejsca startu: dojazd na pierwszy "
         "załadunek to puste kilometry w kosztach i zysku. Zostaw kod pusty, aby liczyć od pierwszego załadunku. "
         "Ten krok jest opcjonalny.")
    o1, o2, o3 = st.columns([1, 2, 4])
    o_country = o1.text_input("Kraj", key="origin_country", value="PL", max_chars=2,
                              help="Dwuliterowy kod kraju, np. PL, DE, FR.").strip().upper()
    o_postal = o2.text_input("Kod pocztowy startu", key="origin_postal", placeholder="np. 62-800",
                             help="Kod pocztowy miejsca, w którym pojazd jest wolny.").strip()
    origin = None
    if o_postal:
        if len(o_country) != 2:
            o3.warning("Podaj dwuliterowy kod kraju (np. PL).")
        else:
            geocoder.prefetch([(o_country, o_postal)])
            o_lat, o_lon, o_src = geocoder.locate(o_country, o_postal)
            geocoder.save()
            origin = Stop(order_id="START", kind="S", country=o_country, postal=o_postal, city="",
                          lat=float(o_lat), lon=float(o_lon))
            if o_src == "kraj":
                o3.warning(f"Nie udało się zlokalizować kodu {o_country} {o_postal} - użyto środka kraju, "
                           "więc dojazd jest bardzo przybliżony. Sprawdź kod lub połączenie z internetem.")
            else:
                o3.success(f"Start: {o_country} {o_postal} ({o_lat:.2f}, {o_lon:.2f}).")
    st.markdown("**Punkty dodatkowe (obowiązkowe)** - miejsca, które trasa musi zahaczyć po drodze (np. magazyn, "
                "warsztat). Każdy trafia w najtańsze miejsce trasy: po starcie, przed ostatnim rozładunkiem. "
                "Opcjonalnie możesz podać **własny ładunek** zbierany w tym punkcie (LDM i/lub waga) oraz **miejsce "
                "zrzutu** - taki ładunek jedzie w każdym wariancie razem ze zleceniami z giełdy (mieści się w limicie "
                "13,6 LDM / 24 t), a cena jest opcjonalna (puste = 0 EUR).")
    wp_df = st.data_editor(
        pd.DataFrame({"Kraj": pd.Series(dtype=str), "Kod pocztowy": pd.Series(dtype=str),
                      "Opis": pd.Series(dtype=str), "Ładunek LDM": pd.Series(dtype=float),
                      "Ładunek (t)": pd.Series(dtype=float), "Cena (EUR)": pd.Series(dtype=float),
                      "Data odbioru": pd.Series(dtype="datetime64[ns]"), "Zrzut: kraj": pd.Series(dtype=str), "Zrzut: kod": pd.Series(dtype=str),
                      "Zrzut: opis": pd.Series(dtype=str)}),
        key="waypoints_editor", num_rows="dynamic", hide_index=True, width="stretch",
        column_config={
            "Kraj": st.column_config.TextColumn("Kraj", max_chars=2, width=55, default="PL",
                                                help="Dwuliterowy kod kraju, np. PL."),
            "Kod pocztowy": st.column_config.TextColumn("Kod", width=110),
            "Opis": st.column_config.TextColumn("Opis (opcjonalnie)", width=160),
            "Ładunek LDM": st.column_config.NumberColumn("LDM", min_value=0.0, max_value=13.6, format="%.1f",
                                                         width=105, help="Pusty = punkt bez ładunku."),
            "Ładunek (t)": st.column_config.NumberColumn("Tony", min_value=0.0, max_value=24.0, format="%.1f",
                                                         width=95, help="Pusty = wyliczone z LDM."),
            "Cena (EUR)": st.column_config.NumberColumn("Cena EUR", min_value=0.0, format="%.0f", width=90,
                                                        help="Przychód za ten ładunek; puste = 0 (własny towar)."),
            "Data odbioru": st.column_config.DateColumn("Data odbioru", format="DD.MM.YYYY", width=115,
                                                        help="Opcjonalnie. Zlecenia z giełdy w wariancie muszą mieć "
                                                             "załadunek w limicie dni od tej daty (ustawienie "
                                                             "„Maks. różnica dat załadunku”)."),
            "Zrzut: kraj": st.column_config.TextColumn("Zrzut kraj", max_chars=2, width=95,
                                                       help="Puste = ten sam kraj co punkt."),
            "Zrzut: kod": st.column_config.TextColumn("Zrzut kod", width=95,
                                                      help="Wymagany, gdy punkt ma ładunek."),
            "Zrzut: opis": st.column_config.TextColumn("Zrzut opis", width=160)})
    waypoints, own_orders, wp_notes = build_extra_points(
        ({"country": r["Kraj"], "postal": r["Kod pocztowy"], "desc": r["Opis"], "ldm": r["Ładunek LDM"],
          "weight": r["Ładunek (t)"], "price": r["Cena (EUR)"], "date": r["Data odbioru"],
          "drop_country": r["Zrzut: kraj"], "drop_postal": r["Zrzut: kod"], "drop_desc": r["Zrzut: opis"]}
         for r in wp_df.to_dict("records")),
        lambda c, p: (geocoder.prefetch([(c, p)]), geocoder.locate(c, p))[1])
    geocoder.save()
    for note in wp_notes:
        st.warning(note)
    if len(waypoints) or len(own_orders):
        st.caption("Punkty dodatkowe: " + ", ".join(
            [f"📍 {w.order_id} {w.country} {w.postal} {w.city}".strip() for w in waypoints]
            + [f"📍 {o.id} {o.start_country} {o.start_postal} ({o.ldm:g} LDM"
               f"{', odbiór ' + o.load_date.strftime('%d.%m.%Y') if o.load_date else ''}) → zrzut {o.end_country} {o.end_postal}"
               for o in own_orders.itertuples()]))
waypoints = tuple(waypoints)
if len(own_orders):                      # własne ładunki: obowiązkowe zlecenia w każdym wariancie
    orders_opt = pd.concat([orders_opt, own_orders.reindex(columns=orders_opt.columns)], ignore_index=True)
    client_map = {**client_map, **dict(zip(own_orders["id"], own_orders["client"]))}
forced_ids = tuple(own_orders["id"]) if len(own_orders) else ()
origin_sig = None if origin is None else (origin.country, origin.postal, round(origin.lat, 4), round(origin.lon, 4))
candidates, stats = _search(orders_opt, cost, opt, origin, waypoints, forced_ids)

# ------------------------------------------------------- krok 3: warianty
with st.container(border=True):
    step(4, "Najlepsze doładunki", "Kombinacje zleceń na jednej naczepie, od najbardziej zyskownej - liczone po "
                                   "rzeczywistych trasach. Kliknij wiersz, aby zobaczyć szczegóły i trasę.")
    if stats["truncated"]:
        st.info("Przy tylu ofertach sprawdzono najlepsze kombinacje, a nie wszystkie możliwe (limit czasu obliczeń). "
                "Wyniki są wiarygodne, ale żeby nie przeoczyć nic ciekawego, zawęź dane (np. filtr, mniejsze odchylenie "
                "trasy) albo zwiększ „Maks. odchylenie” w ustawieniach.")
    if not candidates:
        st.info("Nie znaleziono doładunków spełniających warunki. Potrzeba co najmniej 2 ofert z ceną, "
                "a ich trasy muszą mieścić się w limicie odchylenia - spróbuj go zwiększyć w ustawieniach."
                + (" Masz własny ładunek w punkcie dodatkowym: jest w każdym wariancie, więc sprawdź jego datę "
                   "odbioru (zlecenia z giełdy muszą mieć załadunek w limicie dni od niej), miejsce zrzutu i LDM."
                   if forced_ids else ""))
        st.stop()

    # ranking po trasach rzeczywistych: liczony od razu, pamiętany do zmiany danych lub ustawień
    sig = (analysis["id"], hash(orders.drop(columns=["start_geo", "end_geo"]).to_csv()), cost, opt, provider,
           valhalla_url, ors_key, truck, route_opts, top_n, origin_sig,
           tuple((w.country, w.postal, round(w.lat, 4), round(w.lon, 4)) for w in waypoints),
           hash(own_orders.to_csv()) if len(own_orders) else 0)
    cache = st.session_state.get("ranked")
    if not cache or cache["sig"] != sig:
        bar = st.progress(0.0, text="Wyznaczanie tras po drogach dla najlepszych wariantów…")
        ranked = rank_by_real_routes(
            candidates, cost, lambda x: get_route(x, []), top_n, workers=4,
            progress=lambda done, total: bar.progress(done / total, text=f"Trasy po drogach: {done}/{total}"))
        bar.empty()
        estimated = ranked is None
        if estimated:  # routing niedostępny: ranking szacunkowy (linie proste)
            ranked = [Ranked(x, (r := route_for_variant(x, [], truck, "none", "", valhalla_url, speed, route_opts)),
                             evaluate_route(x, r, cost)) for x in candidates[:top_n]]
        cache = st.session_state.ranked = {"sig": sig, "ranked": ranked, "estimated": estimated}
    ranked, estimated = cache["ranked"], cache["estimated"]
    profitable = [r for r in ranked if r.ev["profit"] > 0]
    n_hidden = len(ranked) - len(profitable)
    if profitable:
        ranked = profitable                  # stratnych wariantów nie pokazujemy
    else:
        st.warning("Wszystkie znalezione warianty są stratne (zysk ≤ 0) - pokazuję je, ale warto zmienić "
                   "limity wyszukiwania albo uzupełnić ceny.")
    if estimated:
        st.warning("Silnik tras jest niedostępny - ranking jest szacunkowy (linia prosta × "
                   f"{ROAD_FACTOR}). Sprawdź ustawienia „Trasy po drogach” w panelu bocznym.")

    best = ranked[0]
    bv, bev = best.variant, best.ev
    st.markdown(f"<div class='best'>🏆 <b>Najlepszy wariant: {len(bv.order_ids)} {plural(len(bv.order_ids))} "
                f"({' + '.join(bv.order_ids)})</b> · zysk <b>{eur(bev['profit'])}</b> · {bev['days']:.1f} dnia · "
                f"{eur(bev['profit'] / max(bev['days'], 1))}/dzień · naczepa {bv.peak_ldm:.1f}/13,6 LDM, "
                f"{bv.peak_weight_t:.1f}/24 t{'' if estimated else ' (trasa po drogach)'}</div>",
                unsafe_allow_html=True)

    vclients = sorted({c for r in ranked for i in r.variant.order_ids if (c := client_map.get(i))})
    vf1, vf2, vf3 = st.columns([2, 2, 2])
    sel_vc = vf1.multiselect("Filtr: zleceniodawca w wariancie", vclients, key="vf_clients", placeholder="Wszyscy",
                             help="Pokazuje warianty, w których jest zlecenie wybranego zleceniodawcy.")
    vq = vf2.text_input("Szukaj w wariantach (zleceniodawca, nr, miasto)", key="vf_q").strip().lower()
    vcodes_txt = vf3.text_input("Kod pocztowy w wariancie", key="vf_codes", placeholder="np. DE 20, FR 77",
                                help="Warianty, w których któryś załadunek lub rozładunek zaczyna się od podanego "
                                     "kodu (z krajem lub bez). Kilka kodów rozdziel przecinkiem.")
    vcodes = parse_codes(vcodes_txt)

    def _variant_text(r) -> str:
        ids = r.variant.order_ids
        return " ".join([*ids, *(client_map.get(i, "") for i in ids),
                         *(f"{s.city} {s.postal} {s.country}" for s in r.variant.stops)]).lower()

    shown = [r for r in ranked
             if (not sel_vc or any(client_map.get(i) in sel_vc for i in r.variant.order_ids))
             and (not vq or vq in _variant_text(r))
             and (not vcodes or any(code_match(x.country, x.postal, vcodes) for x in r.variant.stops if x.kind in ("L", "U")))]
    if not shown:
        st.info("Żaden wariant nie pasuje do filtra - pokazuję wszystkie.")
        shown = ranked
    elif len(shown) < len(ranked):
        st.caption(f"Widok: {len(shown)} z {len(ranked)} wariantów.")

    frame = ranked_to_frame(shown, client_map, est_ids)
    money = {c: st.column_config.NumberColumn(c, format="%d") for c in frame.columns if "EUR" in c or "km" in c.lower()}
    # kolumny bez informacji (np. Trans.eu nie podaje zleceniodawcy) nie zajmują miejsca w tabeli
    blank = [c for c in ("Zleceniodawcy", "Ceny szac.") if (frame[c] == "—").all()]
    all_cols = st.toggle("Pokaż wszystkie kolumny tabeli", key="vt_all",
                         help="Domyślnie tabela pokazuje najważniejsze kolumny: zlecenia, zysk, koszty, dystans, cena/km, czas i trasę.")
    core = ["#", "Zlecenia", "Zysk (EUR)", "Zysk/dzień (EUR)", "Przychód (EUR)", "Koszty (EUR)", "Dystans (km)",
            "Cena/km (EUR)", "Czas (dni)", "Zapełnienie (LDM)", "Trasa"]
    if (frame["Przeprawy i tunele"] != "—").any():
        core.append("Przeprawy i tunele")
    shown_cols = [c for c in frame.columns if c not in blank] if all_cols else [c for c in core if c in frame.columns]
    event = st.dataframe(frame[shown_cols], width="stretch", hide_index=True, on_select="rerun", selection_mode="single-row",
                         key=f"variants_table_{abs(hash((tuple(sel_vc), vq, vcodes_txt)))}", column_config={
                             "#": st.column_config.NumberColumn("#", width="small"),
                             "Liczba": st.column_config.NumberColumn("Liczba", width="small",
                                                                     help="Ile zleceń jedzie na naczepie."),
                             "Zapełnienie (LDM)": st.column_config.NumberColumn("LDM", format="%.1f",
                                                                                help="Maks. zajęte metry ładowne."),
                             "Ładunek (t)": st.column_config.NumberColumn("Ładunek (t)", format="%.1f",
                                                                          help="Maks. masa ładunku na naczepie."),
                             "Trasa": st.column_config.TextColumn(width="large"),
                             "Zleceniodawcy": st.column_config.TextColumn(width="medium"),
                             "Ceny szac.": st.column_config.TextColumn(
                                 help="Zlecenia, których cena jest szacunkowa (oferta bez ceny)."),
                             "Przeprawy i tunele": st.column_config.TextColumn(width="large"),
                             **money,
                             "Cena/km (EUR)": st.column_config.NumberColumn(
                                 "Cena/km (EUR)", format="%.2f",
                                 help="Przychód ze wszystkich zleceń podzielony przez cały przejechany dystans "
                                      "(z pustymi kilometrami).")})
    st.caption(f"Sprawdzono {stats['evaluated']} kombinacji z {stats['candidates']} ofert"
               f"{' (z ' + str(stats['eligible']) + ' kwalifikujących się - zbadano ' + str(stats['candidates']) + ' o najlepszej cenie za km)' if stats['eligible'] > stats['candidates'] else ''}; "
               f"na drogach policzono {len(candidates)} najlepszych"
               f"{', ukryto ' + str(n_hidden) + ' stratnych' if n_hidden and profitable else ''}. 🟢▲ załadunek, 🔴▼ rozładunek. W wariancie maks. {max_orders} zleceń, "
               "a naczepa nie może przekroczyć 13,6 LDM / 24 t w żadnym punkcie trasy.")

    with st.expander("💾 Zapisz ranking pod nazwą / zobacz zapisane rankingi"):
        with st.form("save_ranking_form", border=False):
            r1, r2, r3 = st.columns([3, 2, 1])
            rk_name = r1.text_input("Nazwa rankingu", key="rk_name", placeholder="np. Ranking tydzień 42",
                                    help="Zapisuje widoczną powyżej tabelę wariantów (wartości z chwili zapisu).")
            rk_over = r2.checkbox("Nadpisz, jeśli nazwa już istnieje", key="rk_over")
            rk_click = r3.form_submit_button("💾 Zapisz ranking", width="stretch")
        if rk_click:
            try:
                st.success(f"Zapisano ranking „{_store().save_ranking(rk_name, frame, overwrite=rk_over)}”.")
            except NameExists:
                st.warning(f"Ranking „{rk_name.strip()}” już istnieje - zaznacz „Nadpisz” albo zmień nazwę.")
            except InvalidName as e:
                st.warning(str(e))
        saved_rk = _store().list_tables("ranking")
        if saved_rk.empty:
            st.caption("Brak zapisanych rankingów.")
        else:
            rk_info = {r.name: f"{r.name} · {r.n_rows} wariantów · {r.updated_at.replace('T', ' ')}"
                       for r in saved_rk.itertuples()}
            rk_pick = st.selectbox("Zapisane rankingi", list(rk_info), format_func=rk_info.get, key="rk_pick")
            st.dataframe(_store().load_ranking(rk_pick), width="stretch", hide_index=True)
            if st.button("🗑️ Usuń ten ranking", key="rk_delete"):
                _store().delete("ranking", rk_pick)
                st.rerun()

nav_slot.markdown(
    "<div class='nav'><a href='#krok-2'>2 · Zlecenia</a><a href='#krok-3'>3 · Start i punkty</a>"
    "<a href='#krok-4'>4 · Warianty</a><a href='#krok-5'>5 · Trasa i koszty</a>"
    f"<span class='sum'>{len(orders)} ofert · {n_use} w wyszukiwaniu · najlepszy wariant: {eur(bev['profit'])}</span></div>",
    unsafe_allow_html=True)
selected = event.selection.rows[0] if event.selection.rows else 0
v = shown[min(selected, len(shown) - 1)].variant
vkey = _variant_key(v)
st.session_state.setdefault("vias", {})
st.session_state.setdefault("seen_nonce", set())
# punkty pośrednie należą do konkretnych danych: te same numery zleceń po nowym imporcie to inne trasy
via_key = (hash(orders_opt[["id", "start_lat", "start_lon", "end_lat", "end_lon"]].to_csv()), vkey)
vias = st.session_state.vias.get(via_key, [])

# ----------------------------------------------------- krok 4: trasa i koszty
with st.container(border=True):
    step(5, f"Trasa i koszty: {' + '.join(v.order_ids)}",
         "Przeciągnij fragment niebieskiej linii, aby zmienić przebieg trasy. Punkt pośredni przesuniesz "
         "przeciągnięciem, a usuniesz dwuklikiem.")
    v_clients = ", ".join(dict.fromkeys(c for i in v.order_ids if (c := client_map.get(i))))
    if v_clients:
        st.caption(f"Zleceniodawcy: {v_clients}")
    with st.spinner("Wyznaczanie trasy po drogach…"):
        route = get_route(v, vias)
    ev = evaluate_route(v, route, cost)

    d_est = ev["profit"] - v.profit
    kpis([
        ("Zysk", eur(ev["profit"]), f"{d_est:+,.0f} EUR vs szacunek".replace(",", " "), "pos" if d_est >= 0 else "neg",
         "Przychód minus koszty operacyjne."),
        ("Przychód", eur(ev["revenue"]), None, "", "Suma cen wszystkich zleceń w wariancie."),
        ("Koszty", eur(ev["total"]), None, "", "Paliwo + myto + praca (dni × koszt dnia) + opłaty za przeprawy."),
        ("Km pełne / puste", f"{ev['loaded_km']:,.0f} / {ev['empty_km']:,.0f}".replace(",", " "), None, "",
         "Pełne: z ładunkiem. Puste: między rozładunkiem a kolejnym załadunkiem."),
        ("Cena / km", f"{ev['revenue'] / ev['total_km']:.2f} EUR" if ev["total_km"] else "—", None, "",
         "Przychód podzielony przez cały przejechany dystans (z pustymi kilometrami)."),
        ("Czas", f"{ev['days']:.1f} dnia", f"jazda {ev['drive_h']:.1f} h", "",
         "Dni pracy z limitem 9 h dziennie i odpoczynkiem tygodniowym."),
    ])
    st.caption(f"Koszty: paliwo {eur(ev['fuel'])} + myto {eur(ev['tolls'])} + praca {eur(ev['labor'])}"
               f"{' + przeprawy ' + eur(ev['fees']) if ev['fees'] else ''} "
               f"({ev['days']:.1f} dnia × {day_cost:,.0f} EUR; {handling_h:g} h na każdy załadunek i rozładunek). Naczepa: maks. {v.peak_ldm:.1f} LDM, "
               f"{v.peak_weight_t:.1f} t.")

    st.markdown("<div class='chain'>" + route_chain_html(v) + "</div>", unsafe_allow_html=True)
    event_map = route_map(v, route, vias, map_id=vkey, fit_id=vkey, height=620, key=f"map_{vkey}")
    if event_map and event_map.get("map_id") == vkey and event_map["nonce"] not in st.session_state.seen_nonce:
        st.session_state.seen_nonce.add(event_map["nonce"])
        st.session_state.vias[via_key] = apply_edit(vias, event_map, route)
        st.rerun()
    if vias:
        st.caption(f"Punkty pośrednie: {len(vias)}. Koszty zostały przeliczone dla zmienionej trasy.")
    # komunikaty pod mapą: ich pojawienie się nie może przesuwać mapy w trakcie edycji
    if not route.has_real:
        why = f" ({route.error})" if route.error else ""
        st.warning(f"Brak routingu po drogach{why}. Trasa i koszty są orientacyjne (linia prosta × {ROAD_FACTOR}).")
    elif route.straight_legs:
        legs_txt = ", ".join(str(i + 1) for i in route.straight_legs)
        st.warning(f"Odcinki {legs_txt} (pomarańczowe, przerywane) liczone linią prostą - serwer nie wyznaczył "
                   f"drogi ({route.error}). Przeciągnij ich linię na drogę, by dodać punkt pośredni i podzielić "
                   "odcinek, albo użyj własnego serwera Valhalla.")
    else:
        st.success(f"Trasa po drogach dla ciężarówek ({route.provider}): {route.km:,.0f} km, "
                   f"{route.hours:.1f} h jazdy.")

    # przeprawy, płatne tunele, Szwajcaria
    crossings = ev["crossings"]
    for c in crossings:
        if c.kind in ("channel_tunnel", "channel_ferry"):
            fee = f" Doliczono {eur(channel_fee)}." if channel_fee else " Opłata nie jest wliczona (ustaw ją w panelu bocznym)."
            st.info(f"{c.icon} **Trasa przebiega przez kanał La Manche: {c.name}.**{fee}")
        elif c.kind == "ferry":
            st.info(f"{c.icon} **Trasa zawiera przeprawę promową:** {c.name}. Koszt przeprawy nie jest wliczony.")
        elif c.kind == "alpine_tunnel":
            fee = f" Doliczono {eur(tunnel_fee)}." if tunnel_fee else " Opłata nie jest wliczona (ustaw ją w panelu bocznym)."
            st.warning(f"{c.icon} **Płatny tunel w Alpach: {c.name}.**{fee}")
        elif c.kind == "switzerland":
            st.error(f"{c.icon} **Trasa przebiega przez Szwajcarię** - nie udało się jej ominąć na odcinku "
                     f"{c.leg + 1}. Przeciągnij linię na obejście (np. przez Francję lub Austrię).")
    if not crossings and route.has_real:
        st.caption("Na trasie nie wykryto przepraw promowych ani płatnych tuneli alpejskich "
                   "(wykrywanie na podstawie przebiegu trasy, przybliżone).")

    est_in = [i for i in v.order_ids if i in est_ids]
    if est_in:
        st.warning(f"Przychód zawiera ceny szacunkowe: {', '.join(est_in)} "
                   f"({eur(sum(price_map[i] for i in est_in))} z {eur(ev['revenue'])}). Zweryfikuj je przed ofertą.")

    tab_steps, tab_orders = st.tabs(["Przebieg krok po kroku", "Zlecenia w wariancie"])
    with tab_steps:
        stops_df = stop_table(v, route, orders_opt)
        if (stops_df["Zleceniodawca"] == "—").all():
            stops_df = stops_df.drop(columns="Zleceniodawca")
        st.dataframe(stops_df, width="stretch", hide_index=True, column_config={
            "Krok": st.column_config.NumberColumn("Krok", width=55),
            "Operacja": st.column_config.TextColumn("Operacja", width=135),
            "Zlecenie": st.column_config.TextColumn("Zlecenie", width=80),
            "Miejsce": st.column_config.TextColumn("Miejsce", width=165),
            "Data": st.column_config.TextColumn("Data", width=60),
            "Ładunek": st.column_config.TextColumn("Ładunek", width=135,
                                                   help="Ile ładunku zlecenia jest brane lub zostawiane w tym punkcie."),
            "Na naczepie po": st.column_config.TextColumn(
                "Na naczepie po", width=135, help="Zajęte miejsce i masa po tej operacji (limit 13,6 LDM / 24 t)."),
            "Do następnego": st.column_config.TextColumn("Do następnego", width=130,
                                                         help="Odcinek po drogach do kolejnego punktu."),
            "Od startu (km)": st.column_config.NumberColumn("Od startu (km)", format="%d", width=90)})
    with tab_orders:
        st.caption("Kolumna „Kody do wyszukania” pozwala znaleźć ofertę na giełdzie bez nazwy zleceniodawcy "
                   "(pomagają też LDM, waga i cena).")
        ord_df = order_table(v, orders_opt, est_ids)
        if (ord_df["Zleceniodawca"] == "—").all():
            ord_df = ord_df.drop(columns="Zleceniodawca")
        st.dataframe(ord_df, width="stretch", hide_index=True, column_config={
            "Zlecenie": st.column_config.TextColumn("Zlecenie", width=75),
            "Zleceniodawca": st.column_config.TextColumn("Zleceniodawca", width=130),
            "Kody do wyszukania": st.column_config.TextColumn("Kody do wyszukania", width=170),
            "🟢▲ Załadunek": st.column_config.TextColumn("🟢▲ Załadunek", width=200),
            "🔴▼ Rozładunek": st.column_config.TextColumn("🔴▼ Rozładunek", width=200),
            "LDM": st.column_config.NumberColumn("LDM", format="%.1f", width=50),
            "Waga (t)": st.column_config.NumberColumn("Waga (t)", format="%.1f", width=65),
            "Cena (EUR)": st.column_config.NumberColumn("Cena (EUR)", format="%d", width=85),
            "EUR/km": st.column_config.NumberColumn("EUR/km", format="%.2f", width=65,
                                                     help="Cena zlecenia / jego własny dystans (z oferty lub szacowany)."),
            "Cena z": st.column_config.TextColumn("Cena z", width=70)})
