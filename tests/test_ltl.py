import pandas as pd

from ltl.costs import CostParams, operating_cost, trip_time
from ltl.geo import Geocoder, add_coordinates
from ltl.optimizer import OptimizerParams, find_best_combinations
from ltl.parsers import parse_number, parse_location, parse_orders

TIMOCOM_TAB = (
    "FR 77140 Nemours\tGB G1 1 Glasgow\t12.03.2026\t24,0\t13,60\t1450 km\t1 850 EUR\n"
    "DE 80331 München\tPL 00-001 Warszawa\t13.03.\t5,5\t\t1300 km\t\n"
)
TRANSEU = """IT 44027 Migliaro
12.03.2026 08:00 - 12.03.2026 18:00
DE 80331 München
14.03.2026 08:00
24 t · Plandeka
1250 km
1 200 EUR

PL 00-001 Warszawa
15.03.2026
PL 50-001 Wrocław
16.03.2026
12,5 t · Firanka
300 km
Do negocjacji
"""


def test_numbers():
    assert parse_number("1 200") == 1200
    assert parse_number("13,60") == 13.6
    assert parse_number("1.200,50") == 1200.5
    assert parse_number("1,200.50") == 1200.5
    assert parse_number("1.200") == 1200


def test_location():
    assert parse_location("FR 77140") == ("FR", "77140", "")
    assert parse_location("GB G1 1") == ("GB", "G1 1", "")
    assert parse_location("IT 44027 Migliaro") == ("IT", "44027", "Migliaro")
    assert parse_location("EUR 1200") is None


def test_timocom():
    df = parse_orders(TIMOCOM_TAB, "Timocom")
    assert len(df) == 2
    a, b = df.iloc[0], df.iloc[1]
    assert (a.start_country, a.start_postal, a.end_country, a.end_postal) == ("FR", "77140", "GB", "G1 1")
    assert (a.weight_t, a.ldm, a.distance_km, a.price_eur) == (24.0, 13.6, 1450, 1850)
    assert str(a.load_date) == "2026-03-12"
    assert b.status == "Do negocjacji" and pd.isna(b.price_eur)
    assert b.weight_t == 5.5 and "LDM" in b.defaults_used and 0 < b.ldm < 13.6


def test_transeu():
    df = parse_orders(TRANSEU, "Trans.eu")
    assert len(df) == 2
    a, b = df.iloc[0], df.iloc[1]
    assert (a.start_postal, a.end_postal, a.weight_t, a.distance_km, a.price_eur) == ("44027", "80331", 24, 1250, 1200)
    assert str(a.load_date) == "2026-03-12" and str(a.unload_date) == "2026-03-14"
    assert b.status == "Do negocjacji" and b.weight_t == 12.5


def test_auto_detect():
    assert parse_orders(TRANSEU).source.iloc[0] == "Trans.eu"
    assert parse_orders(TIMOCOM_TAB).source.iloc[0] == "Timocom"


def test_costs():
    p = CostParams()
    t = trip_time(585, 2, p)  # 9 h jazdy + 2 h postoju -> 2 dni
    assert t["days"] == 2
    c = operating_cost(1000, 2, p)
    assert abs(c["fuel"] - 1000 * 0.30 * 1.55) < 1e-6 and abs(c["tolls"] - 200) < 1e-6
    assert trip_time(65 * 120, 2, p)["days"] > trip_time(65 * 56, 2, p)["days"] + 6  # tygodniowy odpoczynek


def _orders(rows):
    df = pd.DataFrame(rows, columns=["id", "slat", "slon", "elat", "elon", "ldm", "weight_t", "price_eur"])
    df = df.rename(columns={"slat": "start_lat", "slon": "start_lon", "elat": "end_lat", "elon": "end_lon"})
    df["load_date"] = None
    for c in ("start_country", "end_country"):
        df[c] = "XX"
    for c in ("start_postal", "end_postal", "start_city", "end_city"):
        df[c] = ""
    return df


def test_optimizer_combines_and_respects_capacity():
    # Berlin -> Paryż; Poznań (blisko Berlina) -> Lyon (blisko trasy); trzecie daleko (Madryt->Lizbona)
    rows = [
        ("A", 52.5, 13.4, 48.9, 2.35, 6.0, 10, 1500),
        ("B", 52.4, 16.9, 45.7, 4.8, 6.0, 10, 1500),
        ("C", 40.4, -3.7, 38.7, -9.1, 6.0, 10, 1500),
    ]
    v, _ = find_best_combinations(_orders(rows), CostParams(), OptimizerParams(max_detour_km=800))
    assert v and set(v[0].order_ids) == {"A", "B"}
    assert all("C" not in x.order_ids for x in v)
    # nie mieści się razem (14 LDM > 13.6), ale można rozdzielić kolejnością A+ A- B+ B-
    rows[0] = ("A", 52.5, 13.4, 48.9, 2.35, 8.0, 10, 1500)
    rows[1] = ("B", 48.9, 2.35, 45.7, 4.8, 8.0, 10, 1500)
    v, _ = find_best_combinations(_orders(rows[:2]), CostParams(), OptimizerParams(max_detour_km=800))
    assert v and [s.kind for s in v[0].stops] == ["L", "U", "L", "U"]
    # za mały limit odchylenia -> brak wariantów
    v, _ = find_best_combinations(_orders(rows[:2]), CostParams(), OptimizerParams(max_detour_km=10))
    assert [x for x in v if x.detour_km > 10] == []


def test_geocoder_offline_fallback():
    g = Geocoder(online=False, cache_path=None)
    df = add_coordinates(parse_orders(TRANSEU, "Trans.eu"), g)
    assert df.start_lat.notna().all() and not df.geo_precise.any()
