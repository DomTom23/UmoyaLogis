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


def test_parser_variants():
    # kolumny rozdzielone spacjami, kraj-kod bez spacji, waga w kg, cena z symbolem €
    spaced = "FR77140 Nemours   GB G1 1 Glasgow   12.03.2026   24000 kg   13,6 ldm   1.450 km   € 1.850\n"
    df = parse_orders(spaced)
    r = df.iloc[0]
    assert (r.start_postal, r.end_postal, r.weight_t, r.ldm, r.distance_km, r.price_eur) == ("77140", "G1 1", 24.0, 13.6, 1450, 1850)
    # obie lokalizacje w jednej komórce, stare oznaczenia krajów, kody wieloczłonowe
    one = "D-80331 München → NL 1234 AB Utrecht | 10 t | 5,0 m | 900 km | 700 EUR"
    r = parse_orders(one).iloc[0]
    assert (r.start_country, r.start_postal, r.start_city) == ("DE", "80331", "München")
    assert (r.end_country, r.end_postal, r.end_city) == ("NL", "1234 AB", "Utrecht")
    assert (r.weight_t, r.ldm, r.distance_km, r.price_eur) == (10, 5.0, 900, 700)
    # liczby bez jednostek w kolumnach (waga, LDM, dystans, cena)
    bare = "PT 1000-001 Lisboa\tES 28001 Madrid\t14.03.\t12\t6\t640\t520\n"
    r = parse_orders(bare).iloc[0]
    assert (r.start_postal, r.weight_t, r.ldm, r.distance_km, r.price_eur) == ("1000-001", 12, 6, 640, 520)
    assert str(r.load_date).endswith("-03-14")


def test_parser_many_blocks_and_noise():
    text = "Oferty (3)\nSortuj: cena\n" + TRANSEU + "\nStrona 1 z 2\n"
    df = parse_orders(text)
    assert list(df.start_postal) == ["44027", "00-001"]
    assert df.iloc[0].price_eur == 1200


# ------------------------------------------------------------ routing / edycja trasy
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import fake_valhalla
from ltl.route_edit import Via, apply_edit, build_points, evaluate_route, route_for_variant
from ltl.routing import TruckProfile, compute_route, decode_polyline, encode_polyline


def _variant():
    rows = [("A", 52.5, 13.4, 48.9, 2.35, 6.0, 10, 1500), ("B", 52.4, 16.9, 45.7, 4.8, 6.0, 10, 1500)]
    v, _ = find_best_combinations(_orders(rows), CostParams(), OptimizerParams(max_detour_km=800))
    return v[0]


def test_polyline_roundtrip():
    pts = [(52.5, 13.4), (48.9, 2.35), (-33.86, 151.2)]
    out = decode_polyline(encode_polyline(pts))
    assert all(abs(a - c) < 1e-6 and abs(b - d) < 1e-6 for (a, b), (c, d) in zip(pts, out))


def test_truck_route_and_costs_with_vias():
    srv, url = fake_valhalla.start()
    v = _variant()
    fake_valhalla.REQUESTS.clear()
    truck = TruckProfile(gross_weight_t=40, height_m=4.0)
    r0 = route_for_variant(v, [], truck, "valhalla", "", url, 65)
    assert r0.is_real and len(r0.legs) == len(v.stops) - 1
    req = fake_valhalla.REQUESTS[-1]
    assert req["costing"] == "truck" and req["costing_options"]["truck"]["weight"] == 40
    assert len(fake_valhalla.REQUESTS) == len(v.stops) - 1  # jedno zapytanie na odcinek
    assert [p["type"] for p in req["locations"]] == ["break", "break"]
    e0 = evaluate_route(v, r0, CostParams())
    assert abs(e0["loaded_km"] + e0["empty_km"] - r0.km) < 1e-6
    # przeciągnięcie fragmentu odcinka 0 -> punkt via, trasa dłuższa, koszty przeliczone
    mid = r0.legs[0].shape[len(r0.legs[0].shape) // 2]
    vias = apply_edit([], {"action": "add_via", "leg": 0, "lat": mid[0] + 3, "lon": mid[1] + 3}, r0)
    r1 = route_for_variant(v, vias, truck, "valhalla", "", url, 65)
    types = [p["type"] for p in fake_valhalla.REQUESTS[-len(v.stops) + 1]["locations"]]
    assert types == ["break", "through", "break"]
    e1 = evaluate_route(v, r1, CostParams())
    assert r1.km > r0.km and e1["total"] > e0["total"] and e1["profit"] < e0["profit"]
    # przesunięcie i usunięcie
    moved = apply_edit(vias, {"action": "move_via", "id": vias[0].id, "lat": mid[0], "lon": mid[1]}, r1)
    assert (moved[0].lat, moved[0].lon) == mid
    assert apply_edit(moved, {"action": "remove_via", "id": moved[0].id}, r1) == []
    srv.shutdown()


def test_routing_fallback_when_unreachable():
    v = _variant()
    r = route_for_variant(v, [], TruckProfile(), "valhalla", "", "http://127.0.0.1:9", 65)
    assert not r.is_real and r.error and r.km > 0 and len(r.legs) == len(v.stops) - 1


def test_via_order_follows_leg_shape():
    v = _variant()
    r = compute_route(build_points(v, []), TruckProfile(), "none")
    shape = r.legs[0].shape
    a, b = shape[-1], shape[0]
    p1 = ((a[0] * 3 + b[0]) / 4 + 1, (a[1] * 3 + b[1]) / 4)
    p2 = ((a[0] + b[0] * 3) / 4 + 1, (a[1] + b[1] * 3) / 4)
    vias = apply_edit([], {"action": "add_via", "leg": 0, "lat": p1[0], "lon": p1[1]}, r)
    vias = apply_edit(vias, {"action": "add_via", "leg": 0, "lat": p2[0], "lon": p2[1]}, r)
    assert (vias[0].lat, vias[1].lat) == (p2[0], p1[0])  # p2 jest bliżej początku odcinka


def test_timocom_vertical_copy():
    """Kopia z Timocom: każde pole w osobnej linii (kraj / kod / miasto / data / ...)."""
    text = (pathlib.Path(__file__).parent / "data" / "timocom_vertical.txt").read_text()
    df = parse_orders(text)
    assert len(df) == 9 and set(df.source) == {"Timocom"}
    a = df.iloc[0]
    assert (a.start_country, a.start_postal, a.start_city) == ("FR", "77140", "Nemours")
    assert (a.end_country, a.end_postal, a.end_city) == ("GB", "G1 1", "Glasgow")
    assert (a.weight_t, a.ldm, a.distance_km, a.price_eur) == (22, 13.6, 1176, 2500)
    assert (str(a.load_date)[5:], str(a.unload_date)[5:]) == ("10-07", "10-09")
    assert df.iloc[1].end_postal == "LS25 2"
    r = df.iloc[2]
    assert (r.weight_t, r.ldm, r.distance_km, r.price_eur) == (1.7, 6.7, 649, 1900)
    assert df.iloc[3].status == "Do negocjacji" and df.iloc[3].distance_km == 645
    assert df.iloc[4].end_postal == "LU71" and df.iloc[4].price_eur == 1500
    assert (df.iloc[5].weight_t, df.iloc[5].price_eur, df.iloc[5].distance_km) == (23.9, 1460, 547)
    assert (df.iloc[6].weight_t, df.iloc[6].ldm, df.iloc[6].price_eur) == (0.05, 0.5, 600)
    assert df.iloc[8].end_postal == "MK42 7AA" and df.iloc[8].status == "Do negocjacji"


# ------------------------------------------------------------ elastyczne wczytywanie
import io
from ltl.ingest import clean_edited, parse_sources, read_uploaded


def test_ingest_csv_xlsx_and_encodings():
    csv_semicolon = "start;cel;data;t;ldm;km;cena\nFR 77140 Nemours;GB G1 1 Glasgow;12.03.2026;22,0;13,6;1176;2500,00 EUR\n"
    df = parse_sources([read_uploaded("o.csv", csv_semicolon.encode("cp1250"))])
    assert len(df) == 1 and df.iloc[0].price_eur == 2500 and df.iloc[0].end_postal == "G1 1"
    book = pd.DataFrame([["FR 77140 Nemours", "GB G1 1 Glasgow", "12.03.2026", "22,0", "13,6", "1176", "2500,00 EUR"]])
    buf = io.BytesIO()
    book.to_excel(buf, index=False, header=False)
    df = parse_sources([read_uploaded("o.xlsx", buf.getvalue())])
    assert len(df) == 1 and df.iloc[0].distance_km == 1176


def test_parse_sources_merges_formats_and_renumbers():
    df = parse_sources([TIMOCOM_TAB, "", TRANSEU])
    assert len(df) == 4 and df.id.is_unique and set(df.source) == {"Timocom", "Trans.eu"}


def test_clean_edited_recomputes_status_and_drops_blank():
    df = parse_sources([TIMOCOM_TAB])
    df.loc[1, "price_eur"] = 900.0          # uzupełniona ręcznie cena
    df.loc[len(df)] = [None] * len(df.columns)  # pusty wiersz dodany w edytorze
    out = clean_edited(df)
    assert len(out) == 2 and list(out.status) == ["OK", "OK"] and out.id.is_unique


def test_valhalla_per_leg_requests_and_split_over_limit():
    srv, url = fake_valhalla.start()
    from ltl.routing import route_valhalla
    # trasa łącznie > 1500 km, ale każdy odcinek < 1500 km: osobne zapytania, brak błędu
    pts = [{"lat": 52.5, "lon": 13.4}, {"lat": 48.9, "lon": 2.35}, {"lat": 45.7, "lon": 4.8}, {"lat": 41.4, "lon": 2.2}]
    fake_valhalla.REQUESTS.clear()
    r = route_valhalla(pts, TruckProfile(), url)
    assert r.is_real and len(r.legs) == 3 and len(fake_valhalla.REQUESTS) == 3 and r.km > 1500
    # pojedynczy odcinek > 1500 km: dzielony na połowy (Lizbona - Warszawa ~ 2800 km)
    far = [{"lat": 38.7, "lon": -9.1}, {"lat": 52.2, "lon": 21.0}]
    r = route_valhalla(far, TruckProfile(), url)
    assert r.is_real and len(r.legs) == 1 and r.km > 3000 and r.straight_legs == []
    # odcinek niemożliwy do podzielenia (>1500 km nawet po 3 podziałach nie dotyczy; tu serwer zawsze odrzuca)
    srv.shutdown()


def test_partial_route_falls_back_per_leg():
    from ltl.routing import route_valhalla, RoutingHTTPError
    import ltl.routing as rt
    orig = rt._valhalla_request

    def flaky(group, truck, base_url):
        if group[0]["lat"] > 60:
            raise RoutingHTTPError("HTTP 400: brak drogi")
        return orig(group, truck, base_url)

    srv, url = fake_valhalla.start()
    rt._valhalla_request = flaky
    try:
        pts = [{"lat": 52.5, "lon": 13.4}, {"lat": 48.9, "lon": 2.35}, {"lat": 61.0, "lon": 2.0}, {"lat": 61.5, "lon": 3.0}]
        r = route_valhalla(pts, TruckProfile(), url)
        assert r.has_real and not r.is_real and r.straight_legs == [2] and len(r.legs) == 3
    finally:
        rt._valhalla_request = orig
        srv.shutdown()
