"""Optymalizator Tras LTL - aplikacja Streamlit.  Uruchomienie: streamlit run app.py"""
from __future__ import annotations

import os

import pandas as pd
import streamlit as st

from ltl.costs import CostParams, profit
from ltl.geo import ROAD_FACTOR, Geocoder, add_coordinates, haversine_km
from ltl.mapview import route_map
from ltl.optimizer import OptimizerParams, find_best_combinations, variants_to_frame
from ltl.parsers import parse_orders
from ltl.route_edit import apply_edit, evaluate_route, route_for_variant
from ltl.routing import VALHALLA_URL, RouteResult, TruckProfile

st.set_page_config(page_title="Optymalizator Tras LTL", page_icon="🚛", layout="wide")


@st.cache_resource
def _geocoder(online: bool) -> Geocoder:
    return Geocoder(online=online)


@st.cache_resource
def _route_store() -> dict:
    """Pamięć podręczna tras; zapisujemy tylko wyniki z prawdziwego routingu."""
    return {}


@st.cache_data(show_spinner="Rozpoznawanie zleceń i geokodowanie kodów pocztowych…")
def _prepare(text: str, online: bool, drop_no_price: bool) -> pd.DataFrame:
    df = parse_orders(text, "Auto", drop_without_price=drop_no_price)  # źródło zawsze wykrywane automatycznie
    return add_coordinates(df, _geocoder(online)) if not df.empty else df


def _variant_key(v) -> str:
    return "|".join(f"{s.order_id}{s.kind}" for s in v.stops)


def get_route(v, vias, force: bool = False) -> RouteResult:
    store = _route_store()
    sig = (_variant_key(v), tuple((x.leg, round(x.lat, 5), round(x.lon, 5)) for x in vias),
           provider, truck, valhalla_url, ors_key)
    if not force and sig in store:
        return store[sig]
    res = route_for_variant(v, vias, truck, provider, ors_key, valhalla_url, speed)
    if res.is_real:
        store[sig] = res
    return res


# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("Dane z giełdy")
    raw_text = st.text_area("Wklej surowe dane Timocom / Trans.eu (Ctrl+V)", height=260,
                            help="Format (Timocom lub Trans.eu) jest rozpoznawany automatycznie.")
    online = st.checkbox("Geokoduj kody pocztowe online", value=True,
                         help="zippopotam.us + Nominatim; bez sieci używane są środki krajów (mało dokładne).")
    drop_no_price = st.checkbox("Pomiń zlecenia bez ceny", value=False)

    st.header("Koszty")
    fuel_price = st.number_input("Cena paliwa (EUR/l)", 0.5, 4.0, 1.55, 0.01)
    consumption = st.number_input("Spalanie (l/100 km)", 10.0, 60.0, 30.0, 0.5)
    toll = st.number_input("Średnie myto (EUR/km)", 0.0, 1.0, 0.20, 0.01)
    day_cost = st.number_input("Koszt dnia pracy (EUR)", 0.0, 1000.0, 300.0, 10.0)
    speed = st.number_input("Średnia prędkość (km/h)", 40.0, 90.0, 65.0, 1.0)

    st.header("Optymalizacja")
    max_detour = st.slider("Maks. odchylenie trasy (km)", 0, 1000, 300, 10)
    max_orders = st.slider("Maks. zleceń w wariancie", 2, 4, 3)
    date_gap = st.number_input("Maks. różnica dat załadunku (dni, 0 = bez limitu)", 0, 14, 3)
    top_n = st.slider("Ile wariantów pokazać", 3, 20, 10)

    st.header("Routing (trasy po drogach)")
    provider_label = st.selectbox("Silnik tras", [
        "Valhalla - profil ciężarówki (bez klucza)", "OpenRouteService - HGV (klucz API)",
        "Brak (linie proste)"])
    provider = {"V": "valhalla", "O": "ors"}.get(provider_label[0], "none")
    valhalla_url = st.text_input("Adres serwera Valhalla", os.environ.get("VALHALLA_URL", VALHALLA_URL)) \
        if provider == "valhalla" else VALHALLA_URL
    ors_key = st.text_input("Klucz API OpenRouteService", os.environ.get("ORS_API_KEY", ""), type="password") \
        if provider == "ors" else ""
    with st.expander("Parametry zestawu (ograniczenia dróg)"):
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

# ------------------------------------------------------------------- main
st.title("🚛 Optymalizator Tras LTL")
st.caption(f"Zestaw: 13,6 LDM / 24 t. Ranking wariantów: dystans szacowany (linia prosta × {ROAD_FACTOR}); "
           "trasa wybranego wariantu: rzeczywista, po drogach dla ciężarówek. "
           "Trasa kończy się na ostatnim rozładunku, bez powrotu.")

if not raw_text.strip():
    st.info("Wklej dane z Timocom lub Trans.eu w panelu bocznym.")
    st.stop()

orders = _prepare(raw_text, online, drop_no_price)
if orders.empty:
    st.error("Nie rozpoznano żadnych zleceń. Upewnij się, że w danych są kody w formacie „kraj kod” "
             "(np. FR 77140, GB G1 1) dla startu i celu każdego zlecenia.")
    st.stop()

est = haversine_km(orders.start_lat, orders.start_lon, orders.end_lat, orders.end_lon) * ROAD_FACTOR
orders["distance_est_km"] = est.round(0)
dist_used = orders["distance_km"].fillna(orders["distance_est_km"])
orders["profit_single_eur"] = [
    round(profit(p, d, 2, cost)["profit"], 0) if pd.notna(p) else None
    for p, d in zip(orders["price_eur"], dist_used)
]

st.subheader(f"Zlecenia ({len(orders)}) · wykryte źródło: {orders['source'].iloc[0]}")
if not orders["geo_precise"].all():
    st.warning("Część kodów pocztowych nie została zgeokodowana - użyto środka kraju, "
               "więc odległości i trasy są mocno przybliżone (kolumny *_geo = 'kraj').")
shown = orders.drop(columns=["start_lat", "start_lon", "end_lat", "end_lon", "geo_precise"])
st.dataframe(shown, width="stretch", hide_index=True)

variants, stats = find_best_combinations(orders, cost, opt)
st.subheader("TOP warianty LTL")
if stats["truncated"]:
    st.warning("Przerwano wyszukiwanie po limicie obliczeń - zawęź odchylenie lub liczbę zleceń.")
if not variants:
    st.info("Brak kombinacji spełniających limity (potrzeba ≥ 2 zleceń z ceną, mieszczących się w limicie odchylenia).")
    st.stop()

# ranking po trasach rzeczywistych (opcjonalnie, na życzenie)
if "reroute" not in st.session_state:
    st.session_state.reroute = {}
if st.button("Przelicz TOP po rzeczywistych trasach", help="Jedno zapytanie routingu na wariant."):
    with st.spinner("Wyznaczanie tras…"):
        for v in variants:
            r = get_route(v, [])
            if r.is_real:
                st.session_state.reroute[_variant_key(v)] = evaluate_route(v, r, cost)
real = st.session_state.reroute
if real and all(_variant_key(v) in real for v in variants):
    variants.sort(key=lambda v: -real[_variant_key(v)]["profit"])

frame = variants_to_frame(variants)
if real and all(_variant_key(v) in real for v in variants):
    frame["Zysk, trasa rzecz. (EUR)"] = [round(real[_variant_key(v)]["profit"], 0) for v in variants]
    frame["Dystans rzecz. (km)"] = [round(real[_variant_key(v)]["total_km"], 0) for v in variants]
    frame["#"] = range(1, len(frame) + 1)
event = st.dataframe(frame, width="stretch", hide_index=True, on_select="rerun", selection_mode="single-row")
selected = event.selection.rows[0] if event.selection.rows else 0
st.caption(f"Przeanalizowano {stats['evaluated']} kombinacji z {stats['candidates']} zleceń. "
           "Kliknij wiersz, aby zobaczyć i edytować trasę.")

v = variants[selected]
vkey = _variant_key(v)
st.session_state.setdefault("vias", {})
st.session_state.setdefault("seen_nonce", set())
vias = st.session_state.vias.get(vkey, [])
route = get_route(v, vias)
ev = evaluate_route(v, route, cost)

if not route.is_real:
    why = f" ({route.error})" if route.error else ""
    st.warning(f"Brak routingu po drogach{why}. Pokazana trasa i koszty są orientacyjne "
               f"(linia prosta × {ROAD_FACTOR}).")
else:
    st.success(f"Trasa po drogach dla ciężarówek · {route.provider} · {route.km:,.0f} km · "
               f"{route.hours:.1f} h jazdy wg silnika tras")

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Zysk", f"{ev['profit']:,.0f} EUR", f"{ev['profit'] - v.profit:+,.0f} vs szacunek")
c2.metric("Przychód", f"{ev['revenue']:,.0f} EUR")
c3.metric("Koszty", f"{ev['total']:,.0f} EUR")
c4.metric("Dystans pełny / pusty", f"{ev['loaded_km']:,.0f} / {ev['empty_km']:,.0f} km")
c5.metric("Czas trwania", f"{ev['days']:.1f} dnia", f"jazda {ev['drive_h']:.1f} h", delta_color="off")
st.caption(f"Koszty: paliwo {ev['fuel']:,.0f} + myto {ev['tolls']:,.0f} + praca {ev['labor']:,.0f} EUR "
           f"({ev['days']:.1f} dnia × {day_cost:,.0f})")

event_map = route_map(v, route, vias, map_id=vkey, fit_id=vkey, height=620, key=f"map_{vkey}")
if event_map and event_map.get("map_id") == vkey and event_map["nonce"] not in st.session_state.seen_nonce:
    st.session_state.seen_nonce.add(event_map["nonce"])
    st.session_state.vias[vkey] = apply_edit(vias, event_map, route)
    st.rerun()
if vias:
    st.caption(f"Punkty pośrednie: {len(vias)} (przeciągaj, dwuklik = usuń). Koszty przeliczono po zmianie trasy.")
