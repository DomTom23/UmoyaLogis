"""Optymalizator Tras LTL - aplikacja Streamlit.  Uruchomienie: streamlit run app.py"""
from __future__ import annotations

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from ltl.costs import CostParams, profit
from ltl.geo import Geocoder, add_coordinates, haversine_km, ROAD_FACTOR
from ltl.mapview import build_map
from ltl.optimizer import OptimizerParams, find_best_combinations, variants_to_frame
from ltl.parsers import parse_orders

st.set_page_config(page_title="Optymalizator Tras LTL", page_icon="🚛", layout="wide")


@st.cache_resource
def _geocoder(online: bool) -> Geocoder:
    return Geocoder(online=online)


@st.cache_data(show_spinner="Geokodowanie kodów pocztowych…")
def _prepare(text: str, source: str, online: bool, drop_no_price: bool) -> pd.DataFrame:
    df = parse_orders(text, source, drop_without_price=drop_no_price)
    return add_coordinates(df, _geocoder(online)) if not df.empty else df


# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("Dane z giełdy")
    source = st.radio("Źródło", ["Auto", "Timocom", "Trans.eu"], horizontal=True)
    raw_text = st.text_area("Wklej surowe dane (Ctrl+V)", height=260)
    online = st.checkbox("Geokoduj kody pocztowe online", value=True,
                         help="api.zippopotam.us; bez sieci używane są środki krajów (mniej dokładnie).")
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

cost = CostParams(day_cost_eur=day_cost, avg_speed_kmh=speed, fuel_l_per_100km=consumption,
                  fuel_price_eur=fuel_price, toll_eur_per_km=toll)
opt = OptimizerParams(max_detour_km=max_detour, max_orders=max_orders, top_n=top_n,
                      max_load_date_gap_days=int(date_gap) or None)

# ------------------------------------------------------------------- main
st.title("🚛 Optymalizator Tras LTL")
st.caption("Zestaw: 13,6 LDM / 24 t. Dystans = linia prosta × "
           f"{ROAD_FACTOR} (MVP). Trasa kończy się na ostatnim rozładunku, bez powrotu.")

if not raw_text.strip():
    st.info("Wklej dane z Timocom lub Trans.eu w panelu bocznym.")
    st.stop()

orders = _prepare(raw_text, source, online, drop_no_price)
if orders.empty:
    st.error("Nie rozpoznano żadnych zleceń. Sprawdź wybrane źródło i format wklejonych danych.")
    st.stop()

# dystans/zysk pojedynczych zleceń
est = haversine_km(orders.start_lat, orders.start_lon, orders.end_lat, orders.end_lon) * ROAD_FACTOR
orders["distance_est_km"] = est.round(0)
dist_used = orders["distance_km"].fillna(orders["distance_est_km"])
orders["profit_single_eur"] = [
    round(profit(p, d, 2, cost)["profit"], 0) if pd.notna(p) else None
    for p, d in zip(orders["price_eur"], dist_used)
]

st.subheader(f"Zlecenia ({len(orders)})")
if not orders["geo_precise"].all():
    st.warning("Część kodów pocztowych nie została zgeokodowana - użyto środka kraju, "
               "więc odległości są mocno przybliżone (kolumny *_geo = 'kraj').")
shown = orders.drop(columns=["start_lat", "start_lon", "end_lat", "end_lon", "geo_precise"])
st.dataframe(shown, width="stretch", hide_index=True)

variants, stats = find_best_combinations(orders, cost, opt)
st.subheader("TOP warianty LTL")
if stats["truncated"]:
    st.warning("Przerwano wyszukiwanie po limicie obliczeń - zawęź odchylenie lub liczbę zleceń.")
if not variants:
    st.info("Brak kombinacji spełniających limity (potrzeba ≥ 2 zleceń z ceną, mieszczących się w limicie odchylenia).")
    st.stop()

frame = variants_to_frame(variants)
event = st.dataframe(frame, width="stretch", hide_index=True,
                     on_select="rerun", selection_mode="single-row")
selected = event.selection.rows[0] if event.selection.rows else 0
st.caption(f"Przeanalizowano {stats['evaluated']} kombinacji z {stats['candidates']} zleceń. "
           "Kliknij wiersz, aby zobaczyć trasę na mapie.")

v = variants[selected]
c1, c2, c3, c4 = st.columns(4)
c1.metric("Zysk", f"{v.profit:,.0f} EUR")
c2.metric("Przychód / koszty", f"{v.revenue:,.0f} / {v.cost['total']:,.0f} EUR")
c3.metric("Dystans pełny / pusty", f"{v.loaded_km:,.0f} / {v.empty_km:,.0f} km")
c4.metric("Czas trwania", f"{v.days:.1f} dnia")
components.html(build_map(v).get_root().render(), height=560)
