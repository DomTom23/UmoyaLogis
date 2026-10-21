"""Optymalizator Tras LTL - aplikacja Streamlit.  Uruchomienie: streamlit run app.py"""
from __future__ import annotations

import os

import pandas as pd
import streamlit as st

from ltl.costs import CostParams, profit
from ltl.geo import ROAD_FACTOR, Geocoder, add_coordinates, haversine_km
from ltl.ingest import clean_edited, parse_sources, read_uploaded
from ltl.mapview import route_map
from ltl.optimizer import OptimizerParams, find_best_combinations, variants_to_frame
from ltl.route_edit import apply_edit, evaluate_route, route_for_variant
from ltl.routing import VALHALLA_URL, RouteResult, TruckProfile
from ltl.sample import SAMPLE_TEXT

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


@st.cache_data(show_spinner="Rozpoznawanie zleceń…")
def _parse(texts: tuple, drop_no_price: bool) -> pd.DataFrame:
    return parse_sources(list(texts), drop_without_price=drop_no_price)  # format każdego źródła wykrywany automatycznie


@st.cache_data(show_spinner="Geokodowanie kodów pocztowych…")
def _coords(df: pd.DataFrame, online: bool) -> pd.DataFrame:
    return add_coordinates(df, _geocoder(online))


@st.cache_data(show_spinner="Szukanie najlepszych doładunków…")
def _search(orders: pd.DataFrame, cost: CostParams, opt: OptimizerParams):
    return find_best_combinations(orders, cost, opt)


def _variant_key(v) -> str:
    return "|".join(f"{s.order_id}{s.kind}" for s in v.stops)


def get_route(v, vias) -> RouteResult:
    store = _route_store()
    sig = (_variant_key(v), tuple((x.leg, round(x.lat, 5), round(x.lon, 5)) for x in vias),
           provider, truck, valhalla_url, ors_key)
    if sig in store:
        return store[sig]
    res = route_for_variant(v, vias, truck, provider, ors_key, valhalla_url, speed)
    if res.has_real:
        store[sig] = res
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

    with st.expander("🔎 Szukanie doładunków", expanded=True):
        max_detour = st.slider("Maks. odchylenie trasy (km)", 0, 1000, 300, 10,
                               help="O ile km trasa połączonych zleceń może być dłuższa od najdłuższego z nich.")
        max_orders = st.slider("Maks. zleceń w wariancie", 2, 4, 3)
        date_gap = st.number_input("Maks. różnica dat załadunku (dni)", 0, 14, 3,
                                   help="0 = bez limitu.")
        top_n = st.slider("Ile wariantów pokazać", 3, 20, 10)

    with st.expander("🛣️ Trasy po drogach"):
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
                  fuel_price_eur=fuel_price, toll_eur_per_km=toll)
opt = OptimizerParams(max_detour_km=max_detour, max_orders=max_orders, top_n=top_n,
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
    for k in ("analysis", "raw_text", "vias", "reroute", "seen_nonce"):
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
        online = o1.checkbox("Ustal położenie kodów pocztowych przez internet", value=True,
                             help="zippopotam.us i Nominatim. Bez tego używane są środki krajów (mało dokładne).")
        drop_no_price = o2.checkbox("Pomiń oferty bez ceny", value=False)
        submitted = st.form_submit_button("🔍 Szukaj doładunków", type="primary", width="stretch")
    b1, b2, _ = st.columns([1, 1, 4])
    b1.button("📋 Wczytaj przykład", on_click=_load_example, width="stretch")
    b2.button("🗑️ Wyczyść", on_click=_clear_all, width="stretch")

if submitted:
    texts = [raw_text] + [read_uploaded(f.name, f.getvalue()) for f in files]
    if any(t.strip() for t in texts):
        prev = st.session_state.get("analysis", {}).get("id", 0)
        st.session_state.analysis = {"id": prev + 1, "texts": tuple(texts), "drop": drop_no_price, "online": online}
        st.session_state.reroute = {}
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

parsed = _parse(analysis["texts"], analysis["drop"])
if parsed.empty:
    st.error("Nie rozpoznano żadnych zleceń. W danych muszą być kody w formacie „kraj kod” "
             "(np. FR 77140, GB G1 1) dla miejsca załadunku i rozładunku każdej oferty.")
    st.stop()

# --------------------------------------------------------- krok 2: zlecenia
with st.container(border=True):
    step(2, "Sprawdź zlecenia",
         f"Rozpoznano {len(parsed)} ofert (źródło: {', '.join(sorted(set(parsed['source'])))}). "
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

variants, stats = _search(orders, cost, opt)

# ------------------------------------------------------- krok 3: warianty
with st.container(border=True):
    step(3, "Najlepsze doładunki", "Kombinacje zleceń na jednej naczepie, od najbardziej zyskownej. "
                                   "Kliknij wiersz, aby zobaczyć trasę.")
    if stats["truncated"]:
        st.warning("Przerwano wyszukiwanie po limicie obliczeń - zmniejsz odchylenie lub liczbę zleceń w wariancie.")
    if not variants:
        st.info("Nie znaleziono doładunków spełniających warunki. Potrzeba co najmniej 2 ofert z ceną, "
                "a ich trasy muszą mieścić się w limicie odchylenia - spróbuj go zwiększyć w ustawieniach.")
        st.stop()

    st.session_state.setdefault("reroute", {})
    real = st.session_state.reroute
    if st.button("🛣️ Przelicz ranking po rzeczywistych drogach",
                 help="Wyznacza trasę po drogach dla każdego wariantu (jedno zapytanie na wariant)."):
        with st.spinner("Wyznaczanie tras…"):
            for v in variants:
                r = get_route(v, [])
                if r.has_real:
                    st.session_state.reroute[_variant_key(v)] = evaluate_route(v, r, cost)
        real = st.session_state.reroute
    ranked_real = bool(real) and all(_variant_key(v) in real for v in variants)
    if ranked_real:
        variants.sort(key=lambda v: -real[_variant_key(v)]["profit"])

    best = variants[0]
    best_profit = real[_variant_key(best)]["profit"] if ranked_real else best.profit
    st.markdown(f"<div class='best'>🏆 <b>Najlepszy wariant: {' + '.join(best.order_ids)}</b> · zysk "
                f"<b>{eur(best_profit)}</b> · {best.days:.1f} dnia · {best_profit / max(best.days, 1):,.0f} EUR/dzień"
                f"{' (trasa po drogach)' if ranked_real else ' (szacunek)'}</div>",
                unsafe_allow_html=True)

    frame = variants_to_frame(variants)
    if ranked_real:
        frame["Zysk po drogach (EUR)"] = [round(real[_variant_key(v)]["profit"], 0) for v in variants]
        frame["Dystans po drogach (km)"] = [round(real[_variant_key(v)]["total_km"], 0) for v in variants]
        frame["#"] = range(1, len(frame) + 1)
    money = {c: st.column_config.NumberColumn(c, format="%d") for c in frame.columns if "EUR" in c or "km" in c}
    event = st.dataframe(frame, width="stretch", hide_index=True, on_select="rerun", selection_mode="single-row",
                         key="variants_table", column_config={"#": st.column_config.NumberColumn("#", width="small"),
                                                              "Trasa": st.column_config.TextColumn(width="large"),
                                                              **money})
    st.caption(f"Sprawdzono {stats['evaluated']} kombinacji z {stats['candidates']} zleceń. "
               "▲ załadunek, ▼ rozładunek. Dystanse w rankingu są szacunkowe (linia prosta × "
               f"{ROAD_FACTOR}), dokładne po wyborze wariantu.")

selected = event.selection.rows[0] if event.selection.rows else 0
v = variants[selected]
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

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Zysk", eur(ev["profit"]), f"{ev['profit'] - v.profit:+,.0f} vs szacunek",
              help="Przychód minus koszty operacyjne.")
    c2.metric("Przychód", eur(ev["revenue"]), help="Suma cen wszystkich zleceń w wariancie.")
    c3.metric("Koszty", eur(ev["total"]), help="Paliwo + myto + praca (dni × koszt dnia).")
    c4.metric("Km pełne / puste", f"{ev['loaded_km']:,.0f} / {ev['empty_km']:,.0f}",
              help="Pełne: z ładunkiem. Puste: między rozładunkiem a kolejnym załadunkiem.")
    c5.metric("Czas", f"{ev['days']:.1f} dnia", f"jazda {ev['drive_h']:.1f} h", delta_color="off",
              help="Dni pracy z limitem 9 h dziennie i odpoczynkiem tygodniowym.")
    st.caption(f"Koszty: paliwo {eur(ev['fuel'])} + myto {eur(ev['tolls'])} + praca {eur(ev['labor'])} "
               f"({ev['days']:.1f} dnia × {day_cost:,.0f} EUR)")

    event_map = route_map(v, route, vias, map_id=vkey, fit_id=vkey, height=620, key=f"map_{vkey}")
    if event_map and event_map.get("map_id") == vkey and event_map["nonce"] not in st.session_state.seen_nonce:
        st.session_state.seen_nonce.add(event_map["nonce"])
        st.session_state.vias[vkey] = apply_edit(vias, event_map, route)
        st.rerun()
    if vias:
        st.caption(f"Punkty pośrednie: {len(vias)}. Koszty zostały przeliczone dla zmienionej trasy.")
