"""Optymalizator Tras LTL - aplikacja Streamlit.  Uruchomienie: streamlit run app.py"""
from __future__ import annotations

import os
import time

import pandas as pd
import streamlit as st

from ltl.costs import CostParams, profit
from ltl.geo import ROAD_FACTOR, Geocoder, add_coordinates, haversine_km
from ltl.ingest import clean_edited, parse_sources, read_uploaded
from ltl.mapview import route_map
from ltl.optimizer import OptimizerParams, find_best_combinations
from ltl.ranking import Ranked, rank_by_real_routes, ranked_to_frame, variant_key
from ltl.route_edit import apply_edit, evaluate_route, route_for_variant
from ltl.routing import VALHALLA_URL, RouteOptions, RouteResult, TruckProfile
from ltl.sample import SAMPLE_TEXT
from ltl.storage import InvalidName, NameExists, Store

st.set_page_config(page_title="Optymalizator Tras LTL", page_icon="🚛", layout="wide")

st.markdown("""
<style>
  .block-container { padding-top: 2rem; max-width: 1400px; }
  .step { display: flex; align-items: center; gap: .6rem; margin: .2rem 0 .4rem 0; font-size: 1.35rem; font-weight: 650; }
  .step .n { background: #1f6feb; color: #fff; width: 1.9rem; height: 1.9rem; border-radius: 50%;
             display: inline-flex; align-items: center; justify-content: center; font-size: 1rem; }
  .hint { opacity: .75; font-size: .92rem; margin-bottom: .6rem; }
  .best { border-left: 5px solid #1a9850; padding: .5rem .9rem; background: rgba(26,152,80,.08);
          border-radius: 6px; margin-bottom: .8rem; }
</style>
""", unsafe_allow_html=True)


def step(n: int, title: str, hint: str = "") -> None:
    st.markdown(f"<div class='step'><span class='n'>{n}</span>{title}</div>", unsafe_allow_html=True)
    if hint:
        st.markdown(f"<div class='hint'>{hint}</div>", unsafe_allow_html=True)


def plural(n: int) -> str:
    return "zlecenie" if n == 1 else "zlecenia" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else "zleceń"


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
def _parse(texts: tuple, drop_no_price: bool) -> pd.DataFrame:
    return parse_sources(list(texts), drop_without_price=drop_no_price)  # format każdego źródła wykrywany automatycznie


@st.cache_data(show_spinner="Geokodowanie kodów pocztowych…")
def _coords(df: pd.DataFrame, online: bool) -> pd.DataFrame:
    return add_coordinates(df, _geocoder(online))


@st.cache_data(show_spinner="Szukanie najlepszych doładunków…")
def _search(orders: pd.DataFrame, cost: CostParams, opt: OptimizerParams):
    return find_best_combinations(orders, cost, opt)


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
        top_n = st.slider("Ile wariantów pokazać", 3, 20, 10)
        n_check = st.slider("Ile kandydatów sprawdzić na drogach", 10, 40, 20,
                            help="Najlepsze warianty wg szacunku są liczone po rzeczywistych trasach, a ranking "
                                 "układany od nowa. Więcej = dokładniej, ale dłużej (publiczny serwer tras).")
        st.caption("Liczba zleceń w wariancie nie jest ograniczona - program ładuje tyle, ile mieści "
                   "się na naczepie (13,6 LDM / 24 t) i w limicie odchylenia.")

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
                  channel_crossing_eur=channel_fee, alpine_tunnel_eur=tunnel_fee)
route_opts = RouteOptions(avoid_switzerland=avoid_ch)
opt = OptimizerParams(max_detour_km=max_detour, top_n=max(n_check, top_n),   # kandydaci do sprawdzenia na drogach
                      max_load_date_gap_days=int(date_gap) or None)

# ------------------------------------------------------------------ nagłówek
st.title("🚛 Optymalizator Tras LTL")
st.caption("Wklej oferty z giełdy, a aplikacja znajdzie najbardziej opłacalne doładunki "
           "(naczepa 13,6 LDM / 24 t, trasa kończy się na ostatnim rozładunku).")

# ------------------------------------------------------------ krok 1: dane
st.session_state.setdefault("uploader_n", 0)
st.session_state.setdefault("raw_text", "")


def _load_example() -> None:
    st.session_state.raw_text = SAMPLE_TEXT


def _clear_all() -> None:
    for k in ("analysis", "raw_text", "vias", "ranked", "seen_nonce", "loaded_name", "save_name"):
        st.session_state.pop(k, None)
    st.session_state.uploader_n += 1


with st.container(border=True):
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
    texts = [raw_text] + [read_uploaded(f.name, f.getvalue()) for f in files]
    if any(t.strip() for t in texts):
        prev = st.session_state.get("analysis", {}).get("id", 0)
        st.session_state.analysis = {"id": prev + 1, "texts": tuple(texts), "drop": drop_no_price, "online": online}
        st.session_state.pop("ranked", None)
    else:
        st.warning("Wklej oferty albo dodaj plik, a potem kliknij „Szukaj doładunków”.")

analysis = st.session_state.get("analysis")
if not analysis:
    with st.expander("ℹ️ Jak to działa", expanded=True):
        st.markdown(
            "1. **Wklej oferty** z giełdy (lub kliknij *Wczytaj przykład*) i naciśnij **Szukaj doładunków**.\n"
            "2. **Sprawdź zlecenia** - popraw błędy, uzupełnij brakujące ceny.\n"
            "3. **Wybierz wariant** z listy najlepszych doładunków.\n"
            "4. **Dopracuj trasę** na mapie: przeciągnij fragment linii, a koszty przeliczą się same.")
    st.stop()

parsed = analysis["df"] if "df" in analysis else _parse(analysis["texts"], analysis["drop"])
if parsed.empty:
    st.error("Nie rozpoznano żadnych zleceń. W danych muszą być kody w formacie „kraj kod” "
             "(np. FR 77140, GB G1 1) dla miejsca załadunku i rozładunku każdej oferty.")
    st.stop()

# --------------------------------------------------------- krok 2: zlecenia
with st.container(border=True):
    step(2, "Sprawdź zlecenia",
         f"{'Wczytano zapisaną tabelę „' + st.session_state['loaded_name'] + '”: ' if st.session_state.get('loaded_name') else 'Rozpoznano '}"
         f"{len(parsed)} ofert (źródło: {', '.join(sorted(set(parsed['source'])) or ['—'])}). "
         "Kliknij komórkę, aby poprawić wartość; wiersze można dodawać i usuwać. Zmiany od razu wchodzą do wyników.")
    edited = st.data_editor(
        parsed, key=f"orders_{analysis['id']}", num_rows="dynamic", width="stretch", hide_index=True,
        disabled=["id", "source", "status", "defaults_used"],
        column_order=["id", "source", "start_country", "start_postal", "start_city", "end_country", "end_postal",
                      "end_city", "load_date", "unload_date", "weight_t", "ldm", "distance_km", "price_eur",
                      "status", "defaults_used"],
        column_config={
            "id": st.column_config.TextColumn("Nr", width="small"),
            "source": st.column_config.TextColumn("Źródło", width="small"),
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
            "status": st.column_config.TextColumn("Status", width="small"),
            "defaults_used": st.column_config.TextColumn(
                "Uzupełniono", help="Pola, których nie było w ofercie i przyjęto wartości domyślne."),
        },
    )
    orders = clean_edited(edited)
    if orders.empty:
        st.warning("Brak poprawnych wierszy - potrzebne są kraj i kod pocztowy załadunku oraz rozładunku.")
        st.stop()

    with st.form("save_orders_form", border=False):
        f1, f2, f3 = st.columns([3, 2, 1])
        save_name = f1.text_input("Nazwa tabeli", key="save_name", placeholder="np. Tydzień 42 - Francja → UK",
                                  help="Zapisuje tabelę z Twoimi poprawkami; można ją później wczytać w kroku 1.")
        overwrite = f2.checkbox("Nadpisz, jeśli nazwa już istnieje", key="save_overwrite")
        save_clicked = f3.form_submit_button("💾 Zapisz tabelę", width="stretch")
    if save_clicked:
        try:
            saved_as = _store().save_orders(save_name, orders, overwrite=overwrite)
            st.session_state.loaded_name = saved_as
            st.success(f"Zapisano tabelę „{saved_as}” ({len(orders)} zleceń).")
        except NameExists:
            st.warning(f"Tabela „{save_name.strip()}” już istnieje - zaznacz „Nadpisz” albo wybierz inną nazwę.")
        except InvalidName as e:
            st.warning(str(e))
    orders = _coords(orders, analysis["online"])

    est = haversine_km(orders.start_lat, orders.start_lon, orders.end_lat, orders.end_lon) * ROAD_FACTOR
    orders["distance_est_km"] = est.round(0)
    dist_used = orders["distance_km"].fillna(orders["distance_est_km"])
    orders["profit_single_eur"] = [round(profit(p, d, 2, cost)["profit"], 0) if pd.notna(p) else None
                                   for p, d in zip(orders["price_eur"], dist_used)]
    n_priced = int(orders["price_eur"].notna().sum())
    if n_priced < len(orders):
        st.info(f"{len(orders) - n_priced} ofert bez ceny („Do negocjacji”) nie bierze udziału w wyszukiwaniu - "
                "wpisz cenę w tabeli, aby je uwzględnić.")
    if not orders["geo_precise"].all():
        st.warning("Części kodów pocztowych nie udało się zlokalizować - użyto środka kraju, więc odległości "
                   "i trasy są mocno przybliżone. Sprawdź połączenie z internetem lub kody pocztowe.")
    with st.expander("Szczegóły: dystans szacowany i zysk pojedynczych zleceń"):
        st.dataframe(
            orders[["id", "start_postal", "end_postal", "price_eur", "distance_km", "distance_est_km",
                    "profit_single_eur", "start_geo", "end_geo"]],
            width="stretch", hide_index=True,
            column_config={"id": "Nr", "start_postal": "Kod zał.", "end_postal": "Kod rozł.",
                           "price_eur": "Cena (EUR)", "distance_km": "Dystans z oferty (km)",
                           "distance_est_km": "Dystans szac. (km)", "profit_single_eur": "Zysk samodzielnie (EUR)",
                           "start_geo": "Pozycja zał.", "end_geo": "Pozycja rozł."})

candidates, stats = _search(orders, cost, opt)

# ------------------------------------------------------- krok 3: warianty
with st.container(border=True):
    step(3, "Najlepsze doładunki", "Kombinacje zleceń na jednej naczepie, od najbardziej zyskownej - liczone po "
                                   "rzeczywistych trasach. Kliknij wiersz, aby zobaczyć trasę.")
    if stats["truncated"]:
        st.warning("Przerwano wyszukiwanie po limicie obliczeń - zmniejsz odchylenie albo liczbę ofert.")
    if not candidates:
        st.info("Nie znaleziono doładunków spełniających warunki. Potrzeba co najmniej 2 ofert z ceną, "
                "a ich trasy muszą mieścić się w limicie odchylenia - spróbuj go zwiększyć w ustawieniach.")
        st.stop()

    # ranking po trasach rzeczywistych: liczony od razu, pamiętany do zmiany danych lub ustawień
    sig = (analysis["id"], hash(orders.drop(columns=["start_geo", "end_geo"]).to_csv()), cost, opt, provider,
           valhalla_url, ors_key, truck, route_opts, top_n)
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
    if estimated:
        st.warning("Silnik tras jest niedostępny - ranking jest szacunkowy (linia prosta × "
                   f"{ROAD_FACTOR}). Sprawdź ustawienia „Trasy po drogach” w panelu bocznym.")

    best = ranked[0]
    bv, bev = best.variant, best.ev
    st.markdown(f"<div class='best'>🏆 <b>Najlepszy wariant: {len(bv.order_ids)} {plural(len(bv.order_ids))} "
                f"({' + '.join(bv.order_ids)})</b> · zysk <b>{eur(bev['profit'])}</b> · {bev['days']:.1f} dnia · "
                f"{bev['profit'] / max(bev['days'], 1):,.0f} EUR/dzień · naczepa {bv.peak_ldm:.1f}/13,6 LDM, "
                f"{bv.peak_weight_t:.1f}/24 t{'' if estimated else ' (trasa po drogach)'}</div>",
                unsafe_allow_html=True)

    frame = ranked_to_frame(ranked)
    money = {c: st.column_config.NumberColumn(c, format="%d") for c in frame.columns if "EUR" in c or "km" in c.lower()}
    event = st.dataframe(frame, width="stretch", hide_index=True, on_select="rerun", selection_mode="single-row",
                         key="variants_table", column_config={
                             "#": st.column_config.NumberColumn("#", width="small"),
                             "Liczba": st.column_config.NumberColumn("Liczba", width="small",
                                                                     help="Ile zleceń jedzie na naczepie."),
                             "Zapełnienie (LDM)": st.column_config.NumberColumn("LDM", format="%.1f",
                                                                                help="Maks. zajęte metry ładowne."),
                             "Ładunek (t)": st.column_config.NumberColumn("Ładunek (t)", format="%.1f",
                                                                          help="Maks. masa ładunku na naczepie."),
                             "Trasa": st.column_config.TextColumn(width="large"),
                             "Przeprawy i tunele": st.column_config.TextColumn(width="large"),
                             **money})
    st.caption(f"Sprawdzono {stats['evaluated']} kombinacji z {stats['candidates']} ofert; na drogach policzono "
               f"{len(candidates)} najlepszych. ▲ załadunek, ▼ rozładunek. Liczba zleceń nie jest ograniczona - "
               "limitem jest naczepa (13,6 LDM / 24 t w każdym punkcie trasy).")

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

selected = event.selection.rows[0] if event.selection.rows else 0
v = ranked[min(selected, len(ranked) - 1)].variant
vkey = _variant_key(v)
st.session_state.setdefault("vias", {})
st.session_state.setdefault("seen_nonce", set())
vias = st.session_state.vias.get(vkey, [])

# ----------------------------------------------------- krok 4: trasa i koszty
with st.container(border=True):
    step(4, f"Trasa i koszty: {' + '.join(v.order_ids)}",
         "Przeciągnij fragment niebieskiej linii, aby zmienić przebieg trasy. Punkt pośredni przesuniesz "
         "przeciągnięciem, a usuniesz dwuklikiem.")
    with st.spinner("Wyznaczanie trasy po drogach…"):
        route = get_route(v, vias)
    ev = evaluate_route(v, route, cost)

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

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Zysk", eur(ev["profit"]), f"{ev['profit'] - v.profit:+,.0f} vs szacunek",
              help="Przychód minus koszty operacyjne.")
    c2.metric("Przychód", eur(ev["revenue"]), help="Suma cen wszystkich zleceń w wariancie.")
    c3.metric("Koszty", eur(ev["total"]), help="Paliwo + myto + praca (dni × koszt dnia) + opłaty za przeprawy.")
    c4.metric("Km pełne / puste", f"{ev['loaded_km']:,.0f} / {ev['empty_km']:,.0f}",
              help="Pełne: z ładunkiem. Puste: między rozładunkiem a kolejnym załadunkiem.")
    c5.metric("Czas", f"{ev['days']:.1f} dnia", f"jazda {ev['drive_h']:.1f} h", delta_color="off",
              help="Dni pracy z limitem 9 h dziennie i odpoczynkiem tygodniowym.")
    st.caption(f"Koszty: paliwo {eur(ev['fuel'])} + myto {eur(ev['tolls'])} + praca {eur(ev['labor'])}"
               f"{' + przeprawy ' + eur(ev['fees']) if ev['fees'] else ''} "
               f"({ev['days']:.1f} dnia × {day_cost:,.0f} EUR). Naczepa: maks. {v.peak_ldm:.1f} LDM, "
               f"{v.peak_weight_t:.1f} t.")

    event_map = route_map(v, route, vias, map_id=vkey, fit_id=vkey, height=620, key=f"map_{vkey}")
    if event_map and event_map.get("map_id") == vkey and event_map["nonce"] not in st.session_state.seen_nonce:
        st.session_state.seen_nonce.add(event_map["nonce"])
        st.session_state.vias[vkey] = apply_edit(vias, event_map, route)
        st.rerun()
    if vias:
        st.caption(f"Punkty pośrednie: {len(vias)}. Koszty zostały przeliczone dla zmienionej trasy.")
