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
    assert abs(c["fuel"] - 1000 * 0.27 * 1.70) < 1e-6 and abs(c["tolls"] - 200) < 1e-6
    assert trip_time(65 * 120, 2, p)["days"] > trip_time(65 * 56, 2, p)["days"] + 6  # tygodniowy odpoczynek


import pytest
import ltl.routing as _rt


@pytest.fixture(autouse=True)
def _clear_route_cache():
    _rt._LEG_CACHE.clear()
    _rt._NO_EXCLUDE.clear()


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
    types = [p["type"] for p in fake_valhalla.REQUESTS[-1]["locations"]]  # reszta odcinków z cache
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

    def flaky(group, truck, base_url, *a):
        if group[0]["lat"] > 60:
            raise RoutingHTTPError("HTTP 400: brak drogi")
        return orig(group, truck, base_url, *a)

    srv, url = fake_valhalla.start()
    rt._valhalla_request = flaky
    try:
        pts = [{"lat": 52.5, "lon": 13.4}, {"lat": 48.9, "lon": 2.35}, {"lat": 61.0, "lon": 2.0}, {"lat": 61.5, "lon": 3.0}]
        r = route_valhalla(pts, TruckProfile(), url)
        assert r.has_real and not r.is_real and r.straight_legs == [2] and len(r.legs) == 3
    finally:
        rt._valhalla_request = orig
        srv.shutdown()


def test_no_order_limit_fills_trailer():
    # 9 małych zleceń (2 LDM / 2 t) na tej samej trasie Berlin->Paryż: naczepa mieści 6 (12 LDM)
    rows = [(f"S{i}", 52.5 + i * 0.02, 13.4, 48.9 + i * 0.02, 2.35, 2.0, 2.0, 300) for i in range(9)]
    v, st = find_best_combinations(_orders(rows), CostParams(), OptimizerParams(max_detour_km=300, hard_cap_orders=8))
    assert v and len(v[0].order_ids) == 6
    assert v[0].peak_ldm <= 13.6 and abs(v[0].peak_ldm - 12.0) < 1e-6
    v4, _ = find_best_combinations(_orders(rows), CostParams(), OptimizerParams(max_detour_km=300, hard_cap_orders=4))
    assert max(len(x.order_ids) for x in v4) == 4
    assert all(x.peak_ldm <= 13.6 + 1e-9 and x.peak_weight_t <= 24 + 1e-9 for x in v)


def test_weight_limit_binds_before_ldm():
    rows = [(f"H{i}", 52.5 + i * 0.02, 13.4, 48.9 + i * 0.02, 2.35, 1.0, 10.0, 400) for i in range(6)]
    v, _ = find_best_combinations(_orders(rows), CostParams(), OptimizerParams(max_detour_km=300))
    assert len(v[0].order_ids) == 2 and v[0].peak_weight_t == 20.0   # 3x10 t > 24 t


# ------------------------------------------------------------ Szwajcaria, przeprawy, tunele, ranking po drogach
from ltl.crossings import detect_crossings
from ltl.ranking import rank_by_real_routes, ranked_to_frame
from ltl.regions import shape_in_switzerland
from ltl.routing import Leg, RouteOptions, RouteResult, route_valhalla


def test_switzerland_avoided_with_detour_and_flagged_when_unavoidable():
    srv, url = fake_valhalla.start()
    paris, milan = {"lat": 48.86, "lon": 2.35}, {"lat": 45.46, "lon": 9.19}
    through_ch = route_valhalla([paris, milan], TruckProfile(), url, 65, RouteOptions(avoid_switzerland=False))
    assert shape_in_switzerland(through_ch.legs[0].shape)              # prosta linia Paryż-Mediolan idzie przez CH
    avoided = route_valhalla([paris, milan], TruckProfile(), url, 65, RouteOptions(avoid_switzerland=True))
    assert avoided.is_real and not shape_in_switzerland(avoided.legs[0].shape) and avoided.switzerland_legs == []
    assert avoided.km >= through_ch.km
    # załadunek i rozładunek w Szwajcarii są dozwolone: bez ostrzeżenia
    bern, zurich = {"lat": 46.95, "lon": 7.45}, {"lat": 47.38, "lon": 8.54}
    r = route_valhalla([bern, zurich], TruckProfile(), url, 65, RouteOptions())
    assert r.switzerland_legs == [] and not any(c.kind == "switzerland" for c in detect_crossings(r))
    r = route_valhalla([paris, bern], TruckProfile(), url, 65, RouteOptions())     # rozładunek w CH
    assert r.switzerland_legs == [] and not any(c.kind == "switzerland" for c in detect_crossings(r))
    # tranzyt bez możliwości objazdu -> ostrzeżenie
    _rt.detour_candidates = lambda *a, **k: []
    try:
        _rt._LEG_CACHE.clear()
        r = route_valhalla([paris, milan], TruckProfile(), url, 65, RouteOptions())
        assert r.switzerland_legs == [0] and any(c.kind == "switzerland" for c in detect_crossings(r))
    finally:
        import importlib
        _rt.detour_candidates = importlib.import_module("ltl.regions").detour_candidates
    srv.shutdown()


def _leg(points, maneuvers=()):
    import numpy as np
    pts = np.asarray(points, float)   # zagęszczenie jak w prawdziwej linii trasy (punkty co ~0.5 km)
    if not maneuvers or maneuvers[0].get("type") != 28:
        dense = [pts[0]]
        for a, b in zip(pts, pts[1:]):
            n = max(2, int(np.hypot(*(b - a)) * 111 / 0.5))
            dense += [a + (b - a) * t for t in np.linspace(0, 1, n)[1:]]
        points = [tuple(p) for p in dense]
    return Leg(shape=points, km=100, hours=1, maneuvers=list(maneuvers))


def test_detect_channel_ferry_tunnel_and_alpine_tunnels():
    ferry = _leg([(50.9, 1.8), (50.967, 1.853), (51.127, 1.333), (51.2, 1.2)], [
        {"type": 28, "names": [], "instruction": "Take the ferry", "begin": (50.967, 1.853), "end": (50.967, 1.853)},
        {"type": 29, "names": [], "instruction": "", "begin": (51.127, 1.333), "end": (51.127, 1.333)}])
    c = detect_crossings(RouteResult(legs=[ferry], is_real=True))
    assert [(x.kind, x.name) for x in c] == [("channel_ferry", "prom Calais–Dover")]
    tunnel = _leg([(50.92, 1.80), (50.93, 1.81), (51.09, 1.13), (51.1, 1.1)])
    assert [x.kind for x in detect_crossings(RouteResult(legs=[tunnel]))] == ["channel_tunnel"]
    alps = _leg([(45.9, 6.8), (45.8667, 6.90), (45.8, 7.0)])
    assert [x.name for x in detect_crossings(RouteResult(legs=[alps]))] == ["Tunel Mont Blanc (FR–IT)"]
    named = _leg([(47.5, 12.0), (47.6, 12.1)], [{"type": 8, "names": ["Tauerntunnel"], "instruction": "", "begin": (47.5, 12.0), "end": (47.6, 12.1)}])
    assert [x.kind for x in detect_crossings(RouteResult(legs=[named]))] == ["alpine_tunnel"]
    plain = _leg([(52.5, 13.4), (52.4, 13.0)])
    assert detect_crossings(RouteResult(legs=[plain])) == []


def test_crossing_fees_in_evaluation():
    v = _variant()
    ferry_route = RouteResult(legs=[_leg([(50.967, 1.853), (51.127, 1.333)], [
        {"type": 28, "names": [], "instruction": "", "begin": (50.967, 1.853), "end": (50.967, 1.853)},
        {"type": 29, "names": [], "instruction": "", "begin": (51.127, 1.333), "end": (51.127, 1.333)}])
        for _ in range(len(v.stops) - 1)], is_real=True)
    base = evaluate_route(v, ferry_route, CostParams())
    priced = evaluate_route(v, ferry_route, CostParams(channel_crossing_eur=250))
    assert priced["fees"] == 250 * len(priced["crossings"]) > 0
    assert abs((base["profit"] - priced["profit"]) - priced["fees"]) < 1e-6


def test_rank_by_real_routes_and_unavailable_routing():
    srv, url = fake_valhalla.start()
    rows = [(f"S{i}", 52.5 + i * 0.02, 13.4, 48.9 + i * 0.02, 2.35, 2.0, 2.0, 300) for i in range(6)]
    cands, _ = find_best_combinations(_orders(rows), CostParams(), OptimizerParams(max_detour_km=300, top_n=8))
    fn = lambda v: route_for_variant(v, [], TruckProfile(), "valhalla", "", url, 65)
    ranked = rank_by_real_routes(cands, CostParams(), fn, top_n=3)
    assert len(ranked) == 3 and ranked[0].ev["profit"] >= ranked[1].ev["profit"] >= ranked[2].ev["profit"]
    frame = ranked_to_frame(ranked)
    assert {"Zapełnienie (LDM)", "Przeprawy i tunele", "Zysk (EUR)"} <= set(frame.columns)
    dead = lambda v: route_for_variant(v, [], TruckProfile(), "valhalla", "", "http://127.0.0.1:9", 65)
    assert rank_by_real_routes(cands, CostParams(), dead, top_n=3) is None
    srv.shutdown()


# ------------------------------------------------------------ baza danych
from ltl.storage import InvalidName, NameExists, Store


def test_store_orders_roundtrip_overwrite_delete(tmp_path):
    st = Store(tmp_path / "t.db")
    df = parse_sources([TIMOCOM_TAB, TRANSEU])
    assert st.save_orders("  Tydzień  42 ", df) == "Tydzień 42"
    out = st.load_orders("Tydzień 42")
    assert len(out) == len(df) and list(out.columns) == list(df.columns)
    assert list(out.id) == list(df.id) and list(out.start_postal) == list(df.start_postal)
    assert out.price_eur.fillna(-1).tolist() == df.price_eur.fillna(-1).tolist()
    assert str(out.load_date.iloc[0]) == str(df.load_date.iloc[0]) and out.status.tolist() == df.status.tolist()
    with pytest.raises(NameExists):
        st.save_orders("Tydzień 42", df)
    st.save_orders("Tydzień 42", df.head(1), overwrite=True)
    assert len(st.load_orders("Tydzień 42")) == 1 and st.list_tables("orders").n_rows.tolist() == [1]
    st.save_ranking("Tydzień 42", pd.DataFrame({"#": [1], "Zysk (EUR)": [500.0]}))   # ten sam napis, inny rodzaj
    assert st.load_ranking("Tydzień 42")["Zysk (EUR)"].tolist() == [500.0]
    assert st.delete("orders", "Tydzień 42") and not st.exists("orders", "Tydzień 42")
    assert st.exists("ranking", "Tydzień 42") and not st.delete("orders", "Tydzień 42")
    for bad in ("", "   ", "x" * 200):
        with pytest.raises(InvalidName):
            st.save_orders(bad, df)
    with pytest.raises(KeyError):
        st.load_orders("brak")


def test_store_persists_between_instances(tmp_path):
    Store(tmp_path / "p.db").save_orders("A", parse_sources([TIMOCOM_TAB]))
    assert len(Store(tmp_path / "p.db").load_orders("A")) == 2


# ------------------------------------------------------------ zleceniodawca, ceny szacunkowe, historia, czas pracy
from ltl.pricing import DEFAULT_RULES, apply_price_rules, frame_to_rules, rules_to_frame
from ltl.costs import trip_time


def test_client_extracted_from_vertical_and_row_formats():
    text = (pathlib.Path(__file__).parent / "data" / "timocom_vertical.txt").read_text()
    df = parse_orders(text)
    assert df.client.tolist() == [
        "Optima Logistics Group S.A.", "Optima Logistics Group S.A.", "Jp Logistic sp. z o.o.",
        "MIT UNS SPEDITION GMBH", '"ADAR" sp. z o.o.', "LKW WALTER Internationale Transportorganisation AG",
        "Interlogis Poland sp. z o.o.", "Interlogis Poland sp. z o.o.", "Interlogis Poland sp. z o.o."]
    row = "FR 77140 Nemours\tGB G1 1 Glasgow\t07.10\t22,00\tPlandeka, Mega\t13,60\t1176\t-\t2500,00 EUR\tOptima Logistics Group S.A.\n"
    r = parse_orders(row).iloc[0]
    assert r.client == "Optima Logistics Group S.A." and r.price_eur == 2500 and r.distance_km == 1176
    # brak firmy i śmieci z paginacji nie są zleceniodawcą
    assert parse_orders(TRANSEU + "\nStrona 1 z 2\n").client.tolist() == ["", ""]


def test_price_rules_estimates_and_minimums():
    rows = [("CH", "FR", 4.0, None), ("CH", "FR", 3.0, None), ("FR", "CH", 2.0, None), ("FR", "CH", 1.9, None),
            ("FR", "GB", 2.0, None), ("FR", "GB", 1.0, None), ("GB", "FR", 4.0, None), ("GB", "FR", 3.0, None),
            ("FR", "DE", 13.6, None), ("FR", "CH", 1.0, 900.0), ("GB", "CH", 4.0, None)]
    df = pd.DataFrame(rows, columns=["start_country", "end_country", "ldm", "price_eur"])
    out = apply_price_rules(df, DEFAULT_RULES)
    assert out.price_used_eur.fillna(-1).tolist() == [600, -1, 500, -1, 600, -1, 600, -1, 2720, 900, 1000]
    assert out.price_kind.tolist() == ["szacunek", "brak", "szacunek", "brak", "szacunek", "brak", "szacunek",
                                       "brak", "szacunek", "oferta", "szacunek"]   # GB->CH: wyższa stawka (250 > 150); FR->DE: BENELUX/DE 200
    assert "poniżej minimum" in out.price_note.iloc[1] and "BENELUX" in out.price_note.iloc[8]
    assert (apply_price_rules(df, DEFAULT_RULES, enabled=False).price_kind != "szacunek").all()
    assert frame_to_rules(rules_to_frame(DEFAULT_RULES)) == DEFAULT_RULES


def test_stop_handling_is_two_hours_per_loading_and_unloading():
    p = CostParams()
    assert p.handling_h_per_stop == 2.0
    t = trip_time(0, 12, p)             # 6 zleceń = 12 przystanków po 2 h = 24 h pracy -> 3 dni po 9 h
    assert t["work_h"] == 24 and t["days"] == 3
    assert OptimizerParams().hard_cap_orders == 6


def test_estimated_prices_enter_variants_only_when_eligible():
    rows = [("A", 52.5, 13.4, 48.9, 2.35, 6.0, 8, 1500), ("B", 52.4, 13.5, 48.8, 2.30, 4.0, 6, None),
            ("C", 52.45, 13.45, 48.85, 2.32, 1.0, 1, None)]
    df = _orders(rows)
    for c in ("start_country", "end_country"):
        df[c] = ["DE", "DE", "DE"]
    df["end_country"] = ["FR", "GB", "GB"]
    priced = apply_price_rules(df, DEFAULT_RULES)       # B: GB, 4 LDM x 300 = 1200 (ok); C: 1 x 300 = 300 (za mało)
    priced["price_eur"] = priced["price_used_eur"]
    v, _ = find_best_combinations(priced, CostParams(), OptimizerParams(max_detour_km=500))
    assert v and set(v[0].order_ids) == {"A", "B"} and all("C" not in x.order_ids for x in v)


def test_history_dedup_filters_and_updates(tmp_path):
    st = Store(tmp_path / "h.db")
    df = parse_orders((pathlib.Path(__file__).parent / "data" / "timocom_vertical.txt").read_text())
    assert st.record_offers(df, "2026-10-01T10:00:00") == {"new": 9, "repeated": 0}
    assert st.record_offers(df, "2026-10-02T10:00:00") == {"new": 0, "repeated": 9}      # bez duplikatów
    changed = df.copy()
    changed.loc[0, "price_eur"] = 2600.0                                                     # inna cena = nowy wpis
    assert st.record_offers(changed, "2026-10-03T10:00:00") == {"new": 1, "repeated": 8}
    h = st.query_history()
    assert len(h) == 10 and h.seen_count.max() == 3
    assert len(st.query_history(clients=["Interlogis Poland sp. z o.o."])) == 3
    assert len(st.query_history(text="walter")) == 1 and len(st.query_history(text="leeds")) == 1
    assert len(st.query_history(to_countries=["GB"], from_countries=["FR"])) == 10
    assert len(st.query_history(date_from="2026-10-03")) == 1 and len(st.query_history(date_to="2026-10-01")) == 9
    row = h[h.client.str.contains("ADAR")].iloc[[0]].copy()
    row["realized_price_eur"], row["outcome"], row["note"] = 1450.0, "zrealizowane", "telefonicznie"
    assert st.update_history(row[["id", "realized_price_eur", "outcome", "note"]]) == 1
    done = st.query_history(outcome="zrealizowane")
    assert len(done) == 1 and done.realized_price_eur.iloc[0] == 1450 and done.note.iloc[0] == "telefonicznie"
    assert st.history_clients()[0] == "Interlogis Poland sp. z o.o."
    assert st.delete_history(list(done.id)) == 1 and len(st.query_history()) == 9


def test_store_migrates_old_database_without_client(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as c:
        c.executescript("""CREATE TABLE saved_tables (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
            name TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, n_rows INTEGER NOT NULL,
            note TEXT NOT NULL DEFAULT '', UNIQUE (kind, name));
            CREATE TABLE saved_orders (table_id INTEGER NOT NULL, row_no INTEGER NOT NULL, order_no TEXT, source TEXT,
            start_country TEXT, start_postal TEXT, start_city TEXT, end_country TEXT, end_postal TEXT, end_city TEXT,
            load_date TEXT, unload_date TEXT, weight_t REAL, ldm REAL, distance_km REAL, price_eur REAL,
            status TEXT, defaults_used TEXT, PRIMARY KEY (table_id, row_no));""")
    st = Store(path)
    st.save_orders("nowa", parse_sources([TIMOCOM_TAB]))
    assert "client" in st.load_orders("nowa").columns


def test_filter_and_merge_edits_keep_hidden_rows():
    from ltl.ordertable import filter_orders, merge_edits
    full = parse_orders((pathlib.Path(__file__).parent / "data" / "timocom_vertical.txt").read_text())
    inter = filter_orders(full, clients=["Interlogis Poland sp. z o.o."])
    assert inter.id.tolist() == ["T7", "T8", "T9"]
    assert filter_orders(full, text="LEEDS").id.tolist() == ["T2"] and filter_orders(full, text="walter").id.tolist() == ["T6"]
    assert filter_orders(full, only_no_price=True).id.tolist() == ["T4", "T8", "T9"]
    edited = inter.copy()
    edited.loc[edited.id == "T8", "price_eur"] = 650.0                      # uzupełniona cena
    edited = edited[edited.id != "T9"]                                      # usunięty wiersz
    edited.loc[len(edited) + 100] = [None] * len(edited.columns)            # nowy wiersz bez numeru
    edited.loc[edited.id.isna(), ["start_country", "start_postal", "end_country", "end_postal", "client"]] = \
        ["FR", "75001", "GB", "SW1A 1AA", "Nowy Klient"]
    merged = merge_edits(full, inter.id.tolist(), edited)
    assert len(merged) == 9 and "T9" not in merged.id.tolist()               # 9 - 1 usunięty + 1 dodany
    assert merged.loc[merged.id == "T8", "price_eur"].iloc[0] == 650
    assert merged.loc[merged.id == "T1", "client"].iloc[0] == "Optima Logistics Group S.A."   # ukryte wiersze zostają
    new = merged[merged.id.str.startswith("M")].iloc[0]
    assert new.client == "Nowy Klient" and merged.id.is_unique


# ------------------------------------------------------------ format kart Trans.eu (prawdziwe oferty), filtry, geokodowanie
from ltl.eligibility import FleetProfile, check_eligibility

CARDS = (pathlib.Path(__file__).parent / "data" / "trans_cards.txt").read_text()


def test_cards_alignment_dates_and_fields():
    df = parse_orders(CARDS)
    assert len(df) == 43 and CARDS.count("29.10.2026\n12:") == 45          # 2 oferty to pętle (ten sam kod)
    first = df.iloc[0]
    assert (first.start_postal, first.end_postal, str(first.load_date), str(first.unload_date)) == \
        ("53021", "67100", "2026-10-30", "2026-11-02")
    assert (first.weight_t, first.ldm, first.distance_km) == (24, 13.6, 921) and first.status == "Do negocjacji"
    assert first.load_type == "FTL" and first.vehicles == "z naczepą" and first.body == "mega, standard, firanka"
    # dwa wiersze dat przy lokalizacji = okno; bierzemy początek okna
    t3 = df[df.start_postal == "38590"].iloc[0]
    assert (str(t3.load_date), str(t3.unload_date), t3.load_type, t3.ldm) == ("2026-10-29", "2026-11-02", "LTL", 1.3)
    # opis zawierający "zał IT42031" nie psuje następnych ofert; opis z dwiema lokalizacjami też
    assert df[df.start_postal == "40121"].end_postal.tolist() == ["13002", "39190"]
    muggio = df[df.start_postal == "20835"].iloc[0]
    assert (muggio.end_postal, muggio.distance_km, muggio.ldm) == ("42230", 507, 0.8) and "Muggio" in muggio.description
    # kod GB bez spacji, kod z "Miasto, Region, Kraj"
    assert {"OX2 6AA", "MK10 9AB", "NN4 7HS", "W1K 3AB", "WF5 9TG"} <= set(df.start_postal) | set(df.end_postal)
    assert df[df.start_postal == "67440"].start_city.iloc[0] == "Wielki Wschód"
    # żaden "zleceniodawca" z terminu płatności ani statusu ceny; kontakt e-mail daje domenę
    assert set(df.client) == {"", "tirolia.at"}
    assert list(df[df.start_postal == "20122"].status) == ["Za duże dla naczepy"]       # 15,4 LDM


def test_cards_prices_currency_sanity_and_flags():
    df = parse_orders(CARDS)
    by = lambda postal: df[df.start_postal == postal].iloc[0]
    assert by("07100").price_eur == 1100 and "ADR" in by("07100").extras
    pln = by("18024")                                  # 4000 PLN przeliczone po kursie 4,25
    assert abs(pln.price_eur - 4000 / 4.25) < 0.01 and "PLN" in pln.defaults_used
    assert abs(parse_orders(CARDS, pln_per_eur=5.0)[lambda d: d.start_postal == "18024"].price_eur.iloc[0] - 800) < 0.01
    assert pd.isna(by("52000").price_eur) and "nierealna" in by("52000").defaults_used            # "1 EUR"
    assert pd.isna(by("52100").price_eur) and "za tonę" in by("52100").defaults_used              # "- PLN/t"
    assert by("76600").status in ("OK", "Do negocjacji", "Za duże dla naczepy")
    huge = df[df.weight_t == 1100].iloc[0]                                                       # literówka "1100 t"
    assert huge.status == "Za duże dla naczepy" and "waga" in huge.defaults_used and "ADR" in huge.extras
    assert "Winda" in by("69500").extras and "Winda" in df[df.weight_t == 1.1].iloc[0].extras


def test_duplicates_dropped_and_counted():
    df = parse_sources([CARDS])
    assert len(df) == 42 and df.id.is_unique
    rocca = df[df.start_postal == "84069"]
    assert len(rocca) == 1 and "×2" in rocca.iloc[0].defaults_used
    assert len(df[df.start_postal == "93600"]) == 2                    # różne ceny = osobne oferty (licytacja)


def test_fleet_eligibility_reasons():
    df = parse_sources([CARDS])
    why = check_eligibility(df, FleetProfile(exclude_cabotage=False))
    row = lambda postal: why[df[df.start_postal == postal].index[0]]
    assert row("69500") == "pojazd bez naczepy, wymaga windy"                                   # bus / solo bez naczepy
    assert why[df[df.end_postal == "39190"].index[0]] == "" and "pojazd bez naczepy" in row("40121")
    assert "nadwozie: chłodnia" in row("06018") and row("07100") == "pojazd bez naczepy, ADR"
    assert "ADR" in row("18024") and "za duże" in row("20122")
    open_fleet = check_eligibility(df, FleetProfile(exclude_cabotage=False, semi_trailer_only=False, accepted_bodies=("firanka", "standard", "mega", "chłodnia", "jumbo"),
                                                    adr=True, tail_lift=True))
    assert (open_fleet == "").sum() > (why == "").sum()
    assert "FTL" in check_eligibility(df, FleetProfile(allow_ftl=False)).iloc[0]


def test_cabotage_excluded_and_benelux_de_rule():
    df = pd.DataFrame([("DE", "DE", 5.0, None), ("DE", "PL", 5.0, None), ("NL", "FR", 2.0, None),
                       ("PL", "PL", 3.0, 300.0), ("FR", "ES", 5.0, None)],
                      columns=["start_country", "end_country", "ldm", "price_eur"])
    why = check_eligibility(df, FleetProfile())
    assert why.tolist() == ["kabotaż (ten sam kraj)", "", "", "kabotaż (ten sam kraj)", ""]
    assert (check_eligibility(df, FleetProfile(exclude_cabotage=False)) == "").all()
    out = apply_price_rules(df, DEFAULT_RULES)
    # DE->DE krajowe: bez szacunku; DE->PL: 5 x 200 = 1000; NL->FR: 400 < 500 -> brak; FR->ES: brak reguły
    assert out.price_used_eur.fillna(-1).tolist() == [-1, 1000, -1, 300, -1]
    rules = frame_to_rules(rules_to_frame(DEFAULT_RULES))
    assert [r for r in rules if "BENELUX" in r.label][0].countries == ("BE", "NL", "LU", "DE")


def test_code_search():
    from ltl.ordertable import code_match, filter_orders, parse_codes
    df = parse_sources([CARDS])
    it = filter_orders(df, from_codes="IT 20")
    assert 0 < len(it) < len(df)
    assert all(p.startswith("20") and c == "IT" for c, p in zip(it.start_country, it.start_postal))
    assert len(filter_orders(df, from_codes="20")) >= len(it)                # bez kraju też działa
    both = filter_orders(df, from_codes="IT", to_codes="FR")
    assert len(both) and set(both.start_country) == {"IT"} and set(both.end_country) == {"FR"}
    assert parse_codes("DE 20, fr-77;\n 00-0") == ["DE20", "FR77", "000"] and code_match("PL", "00-001", ["00"])
    assert len(filter_orders(df, from_codes=" , ")) == len(df)               # puste kody = brak filtra


def test_same_cargo_offered_twice_never_combined():
    rows = [("A1", 52.5, 13.4, 48.9, 2.35, 2.0, 2.0, 900), ("A2", 52.5, 13.4, 48.9, 2.35, 2.0, 2.0, 950),
            ("B", 52.4, 13.5, 48.8, 2.30, 2.0, 2.0, 800)]
    df = _orders(rows)
    df["load_date"] = [pd.Timestamp("2026-10-30").date()] * 3
    v, _ = find_best_combinations(df, CostParams(), OptimizerParams(max_detour_km=500))
    assert v and all(not ({"A1", "A2"} <= set(x.order_ids)) for x in v)
    assert any({"A1", "B"} <= set(x.order_ids) or {"A2", "B"} <= set(x.order_ids) for x in v)


def test_domestic_routes_do_not_get_cross_border_estimates():
    df = pd.DataFrame([("GB", "GB", 13.6, None), ("GB", "FR", 13.6, None)],
                      columns=["start_country", "end_country", "ldm", "price_eur"])
    out = apply_price_rules(df, DEFAULT_RULES)
    assert out.price_kind.tolist() == ["brak", "szacunek"] and out.price_used_eur.iloc[1] == 13.6 * 150


def test_geocoder_prefetch_parallel_and_budget(monkeypatch):
    from ltl.geo import Geocoder
    g = Geocoder(online=True, cache_path=None)
    calls = []
    monkeypatch.setattr(g, "_fetch", lambda c, code: (calls.append(code) or (50.0, 5.0)) if code not in ("99999", "NOM") else None)
    monkeypatch.setattr(g, "_fetch_nominatim", lambda c, p: (51.0, 6.0) if p == "NOM" else None)
    pairs = [("FR", "75001"), ("FR", "75001"), ("GB", "OX2 6AA"), ("GB", "OX2 1AA"), ("FR", "99999"), ("DE", "NOM")]
    ticks = []
    st = g.prefetch(pairs, progress=lambda d, t: ticks.append((d, t)), max_nominatim=5)
    assert sorted(calls) == ["75001", "OX2"] and st["total"] == 4 and ticks[-1] == (4, 4)    # GB: jeden człon = jedno zapytanie
    assert g.locate("GB", "OX2 6AA")[2] == "cache" and g.locate("FR", "99999")[2] == "kraj"


# ------------------------------------------------------------ mapa: uproszczenie linii, odcinki zerowe, bezpieczeństwo etykiet
def test_simplify_shape_keeps_endpoints_and_shape():
    import numpy as np
    from ltl.mapview import simplify_shape
    t = np.linspace(0, 20, 20000)
    shape = np.column_stack([48 + 0.3 * np.sin(t / 3) + 0.0005 * np.cos(50 * t), 2 + t * 0.05])
    out = np.asarray(simplify_shape(shape, tol_m=25))
    assert len(out) < len(shape) / 5 and tuple(out[0]) == tuple(shape[0]) and tuple(out[-1]) == tuple(shape[-1])
    # każdy oryginalny punkt leży blisko uproszczonej linii (tolerancja ~ 25 m + zapas)
    from ltl.geo import haversine_km
    idx = np.searchsorted(out[:, 1], shape[::97, 1]).clip(1, len(out) - 1)
    a, b = out[idx - 1], out[idx]
    frac = np.clip((shape[::97, 1] - a[:, 1]) / (b[:, 1] - a[:, 1]), 0, 1)
    interp = a + (b - a) * frac[:, None]
    assert haversine_km(shape[::97, 0], shape[::97, 1], interp[:, 0], interp[:, 1]).max() < 0.12   # błąd pionowy bywa większy od prostopadłego (25 m)
    short = [(50.0, 10.0), (50.1, 10.1)]
    assert simplify_shape(short) == [[50.0, 10.0], [50.1, 10.1]]


def test_consecutive_stops_in_same_place_need_no_routing_request():
    srv, url = fake_valhalla.start()
    fake_valhalla.REQUESTS.clear()
    pts = [{"lat": 48.86, "lon": 2.35}, {"lat": 48.86, "lon": 2.35}, {"lat": 48.9, "lon": 2.4}]
    r = route_valhalla(pts, TruckProfile(), url)
    assert r.is_real and len(r.legs) == 2 and r.legs[0].km == 0 and len(fake_valhalla.REQUESTS) == 1
    srv.shutdown()


def test_fake_server_ferry_is_detected_as_channel_crossing():
    srv, url = fake_valhalla.start()
    r = route_valhalla([{"lat": 48.86, "lon": 2.35}, {"lat": 51.5, "lon": -0.12}], TruckProfile(), url)
    kinds = [(c.kind, c.name) for c in detect_crossings(r)]
    assert kinds == [("channel_ferry", "prom Calais–Dover")]
    back = route_valhalla([{"lat": 53.4, "lon": -1.5}, {"lat": 49.0, "lon": 2.5}], TruckProfile(), url)
    assert [c.kind for c in detect_crossings(back)] == ["channel_ferry"]
    srv.shutdown()


def test_parse_orders_old_formats_unchanged_by_card_mode():
    # kolumna nie-kartowa: dwa lokalizacje w opisie nie mogą rozbić wiersza tabeli
    row = "FR 77140 Nemours\tGB G1 1 Glasgow\t07.10\t22,00\t13,60\t1176 km\t2 500 EUR\tFirma X (zał. IT42031)\n"
    r = parse_orders(row).iloc[0]
    assert (r.start_postal, r.end_postal, r.price_eur) == ("77140", "G1 1", 2500)


def test_other_ferries_named_by_ports():
    from ltl.regions import port_ferry_name
    assert port_ferry_name((38.22, 15.64), (38.19, 15.55)) == "prom Villa San Giovanni–Messyna"
    assert port_ferry_name((40.92, 9.50), (43.55, 10.30)) == "prom Olbia–Livorno"
    assert port_ferry_name((10.0, 10.0), (11.0, 11.0)) is None
    sicily = _leg([(38.4, 15.8), (38.22, 15.64), (38.19, 15.55), (38.0, 15.4)], [
        {"type": 28, "names": [], "instruction": "", "begin": (38.22, 15.64), "end": (38.22, 15.64)},
        {"type": 29, "names": [], "instruction": "", "begin": (38.19, 15.55), "end": (38.19, 15.55)}])
    c = detect_crossings(RouteResult(legs=[sicily], is_real=True))
    assert [(x.kind, x.name) for x in c] == [("ferry", "prom Villa San Giovanni–Messyna")]


def test_ldm_from_dimensions_in_description():
    from ltl.parsers import ldm_from_dimensions
    cases = {"1 x 420x220x220 / 1000kg": 3.85, "2 x 120x80x220 / 348kg": 0.8, "3 x 100x120x200, 2250kg": 1.5,
             "4 pal : 120*80*143 // 633 KGS TOTAL": 1.6, "7 pal 80*120 + 4 pal 100*120": 4.8,
             "2 pal 1.28 X 0.75 X 1.30) Załadunek: bokiem": 0.8, "1pal 120x80x40 / no change": 0.4,
             "5 x 120x80x190 / 600kg 1:1": 2.0}
    for text, want in cases.items():
        assert abs(ldm_from_dimensions(text) - want) < 0.02, (text, ldm_from_dimensions(text))
    assert ldm_from_dimensions("Załadunek: bokiem, górą") is None and ldm_from_dimensions("FTL CONFIRMED") is None


def test_missing_ldm_estimated_from_description_dimensions_not_only_from_weight():
    df = parse_orders(CARDS)
    row = df[(df.start_postal == "69160")].iloc[0]       # 1 t, "1 x 420x220x220": LDM z wymiarów (3,85), nie z wagi (0,57)
    assert abs(row.ldm - 3.85) < 0.02 and "wymiar" in row.defaults_used
    m = df[df.start_postal == "MK10 9AB"].iloc[0] if (df.start_postal == "MK10 9AB").any() else df[df.end_postal == "93290"].iloc[0]
    assert abs(m.ldm - 1.6) < 0.02                       # "4 pal : 120*80*143"


def test_variant_detail_tables_and_price_per_km():
    from ltl.ranking import Ranked
    from ltl.route_edit import route_for_variant
    from ltl.routing import TruckProfile
    from ltl.variant_detail import order_table, route_chain_html, stop_table
    rows = [("A", 52.5, 13.4, 48.9, 2.35, 6.0, 8, 1500), ("B", 52.4, 13.5, 48.8, 2.30, 4.0, 6, 900)]
    df = _orders(rows)
    df["start_country"], df["end_country"] = ["DE", "DE"], ["FR", "FR"]
    for c, v in (("start_postal", "10115"), ("end_postal", "75001"), ("start_city", "Berlin"), ("end_city", "Paryż")):
        df[c] = v
    df["client"] = ["", "ACME"]
    df["load_date"] = df["unload_date"] = pd.Timestamp("2026-10-30").date()
    df["distance_km"] = float("nan")
    df["distance_est_km"] = [1050.0, 1050.0]
    v, _ = find_best_combinations(df, CostParams(), OptimizerParams(max_detour_km=500))
    route = route_for_variant(v[0], [], TruckProfile(), "none", "", "", 65.0)
    st_df = stop_table(v[0], route, df)
    assert len(st_df) == 4 and st_df["Operacja"].str.contains("🟢▲").sum() == 2 and st_df["Operacja"].str.contains("🔴▼").sum() == 2
    assert st_df["Na naczepie po"].iloc[-1] == "0.0 LDM · 0.0 t"              # po ostatnim rozładunku naczepa pusta
    assert st_df["Do następnego"].iloc[-1] == "—" and st_df["Od startu (km)"].is_monotonic_increasing
    od = order_table(v[0], df, {"A"})
    assert set(od["Zlecenie"]) == {"A", "B"} and od.set_index("Zlecenie").loc["A", "Cena z"] == "szacunek"
    assert od["Kody do wyszukania"].iloc[0] == "DE 10115 → FR 75001" and od["EUR/km"].notna().all()
    html = route_chain_html(v[0])
    assert html.count("#1a9850") == 2 and html.count("#d73027") == 2          # zielone ▲ załadunki, czerwone ▼ rozładunki
    fr = ranked_to_frame([Ranked(v[0], route, evaluate_route(v[0], route, CostParams()))])
    assert fr["Cena/km (EUR)"].iloc[0] == round(fr["Przychód (EUR)"].iloc[0] / fr["Dystans (km)"].iloc[0], 2) or \
        abs(fr["Cena/km (EUR)"].iloc[0] - fr["Przychód (EUR)"].iloc[0] / fr["Dystans (km)"].iloc[0]) < 0.01
    assert fr["Trasa"].iloc[0].count("🟢▲") == 2 and fr["Trasa"].iloc[0].count("🔴▼") == 2


def test_vehicle_origin_adds_empty_leg_and_changes_ranking():
    from ltl.optimizer import Stop
    from ltl.route_edit import route_for_variant
    from ltl.routing import TruckProfile
    from ltl.variant_detail import stop_table
    # A: Berlin -> Paryż, B: Poznań -> Lyon (po drodze), C: Madryt -> Lizbona (daleko od reszty)
    rows = [("A", 52.5, 13.4, 48.9, 2.35, 6.0, 10, 1500), ("B", 52.4, 16.9, 45.7, 4.8, 6.0, 10, 1500),
            ("C", 40.4, -3.7, 38.7, -9.1, 6.0, 10, 1500), ("D", 40.5, -3.6, 38.8, -9.0, 6.0, 10, 1500)]
    df = _orders(rows)
    base, _ = find_best_combinations(df, CostParams(), OptimizerParams(max_detour_km=800))
    far = Stop("START", "S", "PL", "00-001", "", 52.2, 21.0)             # Warszawa: blisko A/B, daleko od C/D
    v, _ = find_best_combinations(df, CostParams(), OptimizerParams(max_detour_km=800), far)
    assert all(x.stops[0].kind == "S" and x.stops[0].order_id == "START" for x in v)
    top = [x for x in v if set(x.order_ids) == {"A", "B"}][0]
    nob = [x for x in base if set(x.order_ids) == {"A", "B"}][0]
    assert top.empty_km > nob.empty_km and top.total_km > nob.total_km and top.profit < nob.profit
    assert len(top.stops) == len(nob.stops) + 1
    cd = [x for x in v if set(x.order_ids) == {"C", "D"}][0]
    assert cd.empty_km > 1000                                         # dojazd z Warszawy do Madrytu
    # koszt obsługi liczony tylko dla załadunków i rozładunków (start nie dolicza 2 h)
    r = route_for_variant(top, [], TruckProfile(), "none", "", "", 65.0)
    ev = evaluate_route(top, r, CostParams())
    assert ev["empty_km"] > 0 and ev["work_h"] == ev["drive_h"] + 4 * CostParams().handling_h_per_stop
    tbl = stop_table(top, r, df.assign(client="", unload_date=None))
    assert tbl["Operacja"].iloc[0].startswith("🏁") and len(tbl) == len(top.stops)


def test_variant_key_depends_on_origin_location():
    from ltl.optimizer import Stop
    from ltl.ranking import variant_key
    rows = [("A", 52.5, 13.4, 48.9, 2.35, 6.0, 10, 1500), ("B", 52.4, 16.9, 45.7, 4.8, 6.0, 10, 1500)]
    df = _orders(rows)
    k = [variant_key(find_best_combinations(df, CostParams(), OptimizerParams(max_detour_km=800), o)[0][0])
         for o in (Stop("START", "S", "PL", "1", "", 52.0, 17.0), Stop("START", "S", "PL", "2", "", 51.0, 19.0), None)]
    assert len(set(k)) == 3


def test_mandatory_waypoint_inserted_at_cheapest_place_and_costed():
    from ltl.optimizer import Stop
    from ltl.ranking import variant_key
    from ltl.route_edit import route_for_variant
    from ltl.routing import TruckProfile
    # A: Berlin -> Paryż, B: Poznań -> Lyon; punkt dodatkowy: Kolonia (po drodze Berlin -> Paryż)
    rows = [("A", 52.5, 13.4, 48.9, 2.35, 6.0, 10, 1500), ("B", 52.4, 16.9, 45.7, 4.8, 6.0, 10, 1500)]
    df = _orders(rows)
    p = OptimizerParams(max_detour_km=800)
    base, _ = find_best_combinations(df, CostParams(), p)
    on_way = Stop("P1", "W", "DE", "50667", "Kolonia", 50.94, 6.96)
    v, _ = find_best_combinations(df, CostParams(), p, None, (on_way,))
    top = [x for x in v if set(x.order_ids) == {"A", "B"}][0]
    kinds = [s.kind for s in top.stops]
    assert kinds.count("W") == 1 and kinds[0] != "W" and kinds[-1] != "W"      # po drodze, nie na końcach
    nob = [x for x in base if set(x.order_ids) == {"A", "B"}][0]
    assert top.total_km >= nob.total_km and top.total_km - nob.total_km < 150   # Kolonia leży prawie po drodze
    far = Stop("P1", "W", "ES", "28001", "Madryt", 40.4, -3.7)
    v2, _ = find_best_combinations(df, CostParams(), p, None, (far,))
    top2 = [x for x in v2 if set(x.order_ids) == {"A", "B"}][0]
    assert top2.total_km - nob.total_km > 1500 and top2.profit < top.profit      # daleki punkt mocno obniża zysk
    assert variant_key(top) != variant_key(top2)
    # postój w punkcie wlicza się do czasu pracy i nie jest liczony jak załadunek
    r = route_for_variant(top, [], TruckProfile(), "none", "", "", 65.0)
    c0, c1 = CostParams(), CostParams(waypoint_stop_h=3.0)
    e0, e1 = evaluate_route(top, r, c0), evaluate_route(top, r, c1)
    assert abs((e1["work_h"] - e0["work_h"]) - 3.0) < 1e-9 and e0["work_h"] == e0["drive_h"] + 4 * c0.handling_h_per_stop
    # start + punkt dodatkowy razem
    start = Stop("START", "S", "PL", "00-001", "", 52.2, 21.0)
    v3, _ = find_best_combinations(df, CostParams(), p, start, (on_way,))
    k3 = [s.kind for s in [x for x in v3 if set(x.order_ids) == {"A", "B"}][0].stops]
    assert k3[0] == "S" and k3.count("W") == 1 and k3[-1] != "W"


def test_password_check_and_db_backup(tmp_path):
    import sqlite3, sys
    from ltl.auth import password_ok
    assert password_ok("tajne", "tajne") and not password_ok("inne", "tajne") and not password_ok("", "")
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "scripts"))
    from backup_db import backup
    st = Store(tmp_path / "x.db")
    df = parse_orders((pathlib.Path(__file__).parent / "data" / "timocom_vertical.txt").read_text())
    st.record_offers(df, "2026-10-01T10:00:00")
    for _ in range(3):
        out = backup(tmp_path / "x.db", tmp_path / "bk", keep=2)
    assert len(list((tmp_path / "bk").glob("*.db"))) <= 3 and Store(out).history_count() == 9
    mode = sqlite3.connect(tmp_path / "x.db").execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal"


def test_waypoint_with_own_cargo_is_forced_into_every_variant():
    from ltl.optimizer import Stop
    from ltl.ownpoints import build_extra_points
    rows = [("A", 52.5, 13.4, 48.9, 2.35, 6.0, 10, 1500), ("B", 52.4, 16.9, 45.7, 4.8, 6.0, 10, 1500),
            ("C", 40.4, -3.7, 38.7, -9.1, 6.0, 10, 1500), ("D", 52.5, 13.5, 48.8, 2.4, 11.0, 18, 1800)]
    df = _orders(rows)
    stops, own, notes = build_extra_points(
        [{"country": "DE", "postal": "50667", "desc": "Kolonia", "ldm": 3, "price": 400,
          "drop_country": "FR", "drop_postal": "75001"}],
        lambda c, p: (50.94, 6.96, "cache") if c == "DE" else (48.86, 2.34, "cache"))
    assert not stops and not notes and list(own["id"]) == ["P1"] and own.loc[0, "ldm"] == 3
    own = own.reindex(columns=df.columns.union(own.columns))
    full = pd.concat([df, own], ignore_index=True)
    v, _ = find_best_combinations(full, CostParams(), OptimizerParams(max_detour_km=800), None, (), ("P1",))
    assert v and all("P1" in x.order_ids for x in v)                       # własny ładunek jest w każdym wariancie
    assert all(x.peak_ldm <= 13.6 + 1e-9 for x in v)
    assert not any({"D", "P1"} <= set(x.order_ids) and x.peak_ldm > 13.6 for x in v)   # D (11 LDM) + 3 LDM mieści się tylko po rozładunku
    assert all(x.revenue >= 400 for x in v)
    # odbiór własnego ładunku jest w punkcie dodatkowym, a zrzut po nim
    t = [x for x in v if set(x.order_ids) >= {"A", "P1"}][0]
    keys = [(s.order_id, s.kind) for s in t.stops]
    assert keys.index(("P1", "L")) < keys.index(("P1", "U"))
    # bez ceny = 0 EUR, ładunek za duży / bez zrzutu = zwykły przystanek z uwagą
    s2, own2, n2 = build_extra_points(
        [{"country": "PL", "postal": "00-001", "ldm": 5}, {"country": "PL", "postal": "00-002", "ldm": 20,
                                                           "drop_postal": "10-001"}, {"postal": "00-003"}],
        lambda c, p: (52.0, 21.0, "cache"))
    assert len(s2) == 3 and own2.empty and len(n2) == 2
    # tylko własny ładunek + jedno zlecenie z giełdy też daje wariant
    v1, _ = find_best_combinations(pd.concat([df.iloc[[0]], own], ignore_index=True), CostParams(),
                                   OptimizerParams(max_detour_km=800), None, (), ("P1",))
    assert v1 and set(v1[0].order_ids) == {"A", "P1"}


def test_file_formats_tabular_utf16_and_source_report():
    import datetime, io
    from ltl.ingest import UnreadableFile, parse_sources_report, read_uploaded
    vert = (pathlib.Path(__file__).parent / "data" / "timocom_vertical.txt").read_text()
    # UTF-16 (Excel „Tekst Unicode”, Notatnik) - kiedyś dawało 0 ofert
    assert len(parse_sources([read_uploaded("a.txt", vert.encode("utf-16"))])) == 9
    assert len(parse_sources([read_uploaded("a.txt", vert.encode("utf-16-le"))])) == 9
    # arkusz: kraj, kod i miasto w osobnych kolumnach, data Excela, waga/LDM/cena, klient - rozpoznane po nagłówkach
    df = pd.DataFrame([["DE", "20095", "Hamburg", "Polska", "00-001", "Warszawa", datetime.date(2026, 10, 30), 5.2, 3.5, 900.0, "ACME"],
                       ["FR", "67100", "Strasburg", "IT", "20122", "Mediolan", datetime.date(2026, 10, 31), 8.0, 6.0, None, "Beta"]],
                      columns=["Kraj zał", "Kod zał", "Miasto zał", "Kraj rozł", "Kod rozł", "Miasto rozł", "Data załadunku",
                               "Waga (t)", "LDM", "Cena (EUR)", "Zleceniodawca"])
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    out = parse_sources([read_uploaded("o.xlsx", buf.getvalue())])
    assert len(out) == 2 and out.start_country.tolist() == ["DE", "FR"] and out.end_country.tolist() == ["PL", "IT"]
    assert out.end_postal.tolist() == ["00-001", "20122"] and out.ldm.tolist() == [3.5, 6.0]
    assert out.price_eur.tolist()[0] == 900.0 and pd.isna(out.price_eur.iloc[1]) and out.client.tolist() == ["ACME", "Beta"]
    assert str(out.load_date.iloc[0]) == "2026-10-30"
    # csv ze średnikami w cp1250, ceny w PLN z nagłówka
    csv_bytes = "Załadunek;Rozładunek;Waga (kg);LDM;Cena PLN\nDE 20095 Hamburg;PL 00-001 Warszawa;5200;3,5;4250\n".encode("cp1250")
    one = parse_sources([read_uploaded("o.csv", csv_bytes)])
    assert len(one) == 1 and one.weight_t.iloc[0] == 5.2 and abs(one.price_eur.iloc[0] - 1000.0) < 1
    # raport źródeł: powtórzenia z innego źródła pomijane, puste źródło nie psuje wyniku
    both, rep = parse_sources_report([("Wklejony tekst", vert), ("plik.txt", vert), ("pusty.txt", "")])
    assert len(both) == 9 and rep[0]["kept"] == 9 and rep[1]["found"] == 9 and rep[1]["kept"] == 0 and rep[2]["empty"]
    _, rep2 = parse_sources_report([("śmieci.txt", "to nie są oferty\nnic tu nie ma")])
    assert rep2[0]["found"] == 0 and not rep2[0]["empty"]
    try:
        read_uploaded("zly.xlsx", b"to nie jest arkusz")
        raise AssertionError("powinien być błąd")
    except UnreadableFile as e:
        assert "arkusz" in str(e)
    # wklejka z giełdy zapisana jako .txt z tabulacjami (bez nagłówków) zostaje bez zmian
    tab_txt = "DE 20095 Hamburg\tPL 00-001 Warszawa\t30.10.2026\t5,2 t\t900 EUR"
    assert read_uploaded("w.txt", tab_txt.encode()) == tab_txt


def test_own_cargo_pickup_date_limits_matching_orders():
    import datetime
    from ltl.ownpoints import build_extra_points
    rows = [("A", 52.5, 13.4, 48.9, 2.35, 6.0, 10, 1500), ("B", 52.4, 16.9, 45.7, 4.8, 6.0, 10, 1500)]
    df = _orders(rows)
    df["load_date"] = [datetime.date(2026, 11, 2), datetime.date(2026, 11, 20)]
    _, own, _ = build_extra_points([{"country": "DE", "postal": "50667", "ldm": 2, "date": "2026-11-03",
                                     "drop_country": "FR", "drop_postal": "75001"}], lambda c, p: (50.9, 7.0, "cache"))
    assert own.loc[0, "load_date"] == datetime.date(2026, 11, 3)
    full = pd.concat([df, own.reindex(columns=df.columns.union(own.columns))], ignore_index=True)
    v, _ = find_best_combinations(full, CostParams(), OptimizerParams(max_detour_km=2000), None, (), ("P1",))
    # B ma załadunek 17 dni po odbiorze własnego ładunku (limit 3 dni): nigdy razem z P1
    assert v and all("B" not in x.order_ids for x in v) and any("A" in x.order_ids for x in v)
    _, own2, _ = build_extra_points([{"postal": "50667", "country": "DE", "ldm": 2, "drop_postal": "75001", "drop_country": "FR"}],
                                    lambda c, p: (50.9, 7.0, "cache"))
    assert own2.loc[0, "load_date"] is None          # bez daty: bez ograniczenia


def test_own_cargo_date_two_digit_year_and_invalid_values():
    import datetime
    from ltl.ownpoints import _date
    assert _date(datetime.date(26, 11, 2)) == datetime.date(2026, 11, 2)
    assert _date(pd.Timestamp("2026-11-02")) == datetime.date(2026, 11, 2)
    assert _date("2026-11-02") == datetime.date(2026, 11, 2)
    assert _date(None) is None and _date(float("nan")) is None and _date(pd.NaT) is None and _date("to nie data") is None


def test_near_duplicates_same_codes_similar_codes_weight_ldm_and_date():
    import datetime, time
    from ltl.duplicates import DupParams, find_duplicates, is_duplicate, prefix, similar_place

    def r(i, sc, sp, ec, ep, w, l, d=datetime.date(2026, 11, 2), city_a="", city_b=""):
        return dict(id=i, start_country=sc, start_postal=sp, start_city=city_a, end_country=ec, end_postal=ep,
                    end_city=city_b, weight_t=w, ldm=l, load_date=d)

    df = pd.DataFrame([r("A", "DE", "20095", "PL", "00-001", 5, 3, city_a="Hamburg", city_b="Warszawa"),
                       r("B", "DE", "20095", "PL", "00-001", 5.2, 3.1, city_a="Altona", city_b="Mokotów"),   # inne miejscowości
                       r("C", "DE", "20099", "PL", "00-001", 5, 3),            # odbiór: podobny kod, zrzut identyczny
                       r("D", "DE", "20095", "PL", "00-002", 5, 3),            # odbiór identyczny, zrzut podobny
                       r("E", "DE", "30095", "PL", "00-001", 5, 3),            # inny rejon odbioru
                       r("F", "DE", "20095", "PL", "00-001", 9, 3),            # inna waga
                       r("G", "DE", "20095", "PL", "00-001", 5, 6),            # inny LDM
                       r("H", "DE", "20095", "PL", "00-001", 5, 3, d=datetime.date(2026, 11, 5)),   # inna data
                       r("I", "DE", "21095", "PL", "01-001", 5, 3),            # oba końce tylko "podobne" (inne rejony)
                       r("J", "DE", "20095", "FR", "00-001", 5, 3)])           # inny kraj zrzutu
    prio = pd.Series([100, 300, 10, 50, 10, 10, 10, 10, 10, 10], index=df.index)
    dup = find_duplicates(df, prio)
    assert dup == {0: "B", 2: "B", 3: "B"}                     # zostaje B (najwyższy priorytet), reszta to duplikaty B
    assert find_duplicates(df, prio, DupParams(enabled=False)) == {}
    assert find_duplicates(df, prio, DupParams(tolerance_pct=0.0, weight_abs_t=0.0, ldm_abs=0.0)).get(0) is None   # B != A przy zerowej tolerancji
    assert find_duplicates(df, prio, DupParams(prefix_len=5)).keys() == {0}                                          # ostry próg: tylko A~B
    # wyższy priorytet (kwalifikuje się do zestawu) wygrywa nawet z droższą ofertą
    assert find_duplicates(df.head(2), pd.Series([1e9, 900.0], index=df.index[:2])) == {1: "A"}
    # GB: porównanie po części przed spacją, kody krótsze niż próg muszą być identyczne
    assert prefix("GB", "WF5 9TG", 3) == "WF5" and similar_place("GB", "WF5 9TG", "GB", "WF5 1AB", 3)
    assert not similar_place("GB", "WF5 9TG", "GB", "WF6 9TG", 3) and not similar_place("PL", "00-001", "DE", "00001", 3)
    assert similar_place("BE", "1000", "BE", "1000", 5) and not similar_place("BE", "1000", "BE", "1001", 5)
    # szybkość: 700 ofert
    big = pd.concat([df.assign(id=df.id + str(k)) for k in range(70)], ignore_index=True)
    t = time.time()
    find_duplicates(big, pd.Series(0.0, index=big.index))
    assert time.time() - t < 5


def _win(day, h1, h2):
    from ltl.schedule import to_h
    import datetime as _dt
    return (to_h(_dt.datetime(2026, 11, day, h1)), to_h(_dt.datetime(2026, 11, day, h2)))


def test_schedule_simulation_rest_waiting_windows_and_tolerance():
    from ltl.schedule import ScheduleParams, StopSpec, from_h, simulate
    p = ScheduleParams()
    L = StopSpec("L", "A", _win(2, 8, 12), 2)
    U = StopSpec("U", "A", _win(3, 6, 18), 2)
    r = simulate([13.0], [L, U], p)                                   # 2 h obsługi + 13 h jazdy: odpoczynek dobowy w drodze
    assert r.feasible and from_h(r.stops[0].arrive_h).hour == 8 and r.stops[1].rest_h == 11.0
    assert from_h(r.stops[1].arrive_h).strftime("%d %H:%M") == "03 10:00" and abs(r.total_h - 28) < 1e-6
    # przyjazd przed otwarciem okna = czekanie; za późno = niezgodne z terminami, tolerancja to łagodzi
    early = simulate([1.0], [StopSpec("S", "S"), StopSpec("L", "A", _win(2, 8, 12), 2)], ScheduleParams(start_at=from_h(_win(2, 6, 7)[0])))
    assert early.feasible and early.stops[1].wait_h == 1.0            # start 6:00 + 1 h jazdy = 7:00, okno od 8:00
    assert simulate([5.0], [StopSpec("S", "S"), StopSpec("L", "A", _win(2, 8, 12), 2)],
                    ScheduleParams(start_at=from_h(_win(2, 8, 9)[0]))).feasible is False   # 8:00 + 5 h = 13:00 > 12:00
    assert simulate([5.0], [StopSpec("S", "S"), StopSpec("L", "A", _win(2, 8, 12), 2)],
                    ScheduleParams(start_at=from_h(_win(2, 8, 9)[0]), tolerance_h=1.5)).feasible
    # limity jazdy: 56 h w tygodniu -> odpoczynek tygodniowy
    long = simulate([20.0] * 4, [StopSpec("S", "S")] + [StopSpec("W", f"P{i}") for i in range(4)], ScheduleParams(start_at=from_h(_win(2, 6, 7)[0])))
    assert long.rest_h >= 48 and long.stops[-1].rest_h > 0 or long.rest_h >= 48
    # start automatyczny: pojazd rusza tak, by zdążyć na początek pierwszego okna (bez czekania)
    auto = simulate([6.0], [StopSpec("S", "S"), StopSpec("L", "A", _win(5, 8, 12), 2)], p)
    assert from_h(auto.stops[1].arrive_h).strftime("%d %H:%M") == "05 08:00" and auto.stops[1].wait_h == 0
    assert from_h(auto.start_h).strftime("%d %H:%M") == "05 02:00"


def test_order_windows_from_columns_and_date_only_defaults():
    import datetime as _dt
    from ltl.schedule import ScheduleParams, from_h, order_windows, resolve_window, sync_windows
    p = ScheduleParams()
    df = pd.DataFrame([{"id": "A", "load_date": _dt.date(2026, 11, 2), "unload_date": _dt.date(2026, 11, 3),
                        "load_from": pd.Timestamp("2026-11-02 08:00"), "load_to": pd.Timestamp("2026-11-02 15:00"),
                        "unload_from": pd.NaT, "unload_to": pd.NaT},
                       {"id": "B", "load_date": _dt.date(2026, 11, 2), "unload_date": None,
                        "load_from": pd.Timestamp("2026-11-02 00:00:00"), "load_to": pd.Timestamp("2026-11-04 23:59:59"),
                        "unload_from": pd.NaT, "unload_to": pd.NaT}])
    w = order_windows(df, p)
    assert (from_h(w["A"]["L"][0]).hour, from_h(w["A"]["L"][1]).hour) == (8, 15)       # godziny z oferty
    assert (from_h(w["A"]["U"][0]).hour, from_h(w["A"]["U"][1]).hour) == (6, 18)       # sama data = godziny przeładunków
    assert from_h(w["B"]["L"][1]).strftime("%d %H") == "04 18" and w["B"]["U"] is None   # zakres dat, brak rozładunku = bez okna
    assert resolve_window(None, None, None, p) is None
    # ręczna zmiana daty unieważnia stare godziny z oferty
    changed = df.copy()
    changed.loc[0, "load_date"] = _dt.date(2026, 11, 9)
    assert pd.isna(sync_windows(changed).loc[0, "load_from"])


def test_cards_give_time_windows_and_storage_roundtrip(tmp_path):
    df = parse_sources([CARDS])
    first = df[df.start_postal == "53021"].iloc[0]                       # "30.10, 13:00" ... "02.11, 08:00"
    assert str(first.load_from) == "2026-10-30 13:00:00" and str(first.unload_from) == "2026-11-02 08:00:00"
    two = df[df.start_postal == "57800"].iloc[0]                         # "30.10, 08:00 - 15:00"
    assert (str(two.load_from), str(two.load_to)) == ("2026-10-30 08:00:00", "2026-10-30 15:00:00")
    ranged = df[df.start_postal == "38590"].iloc[0]                      # dwie linie dat: 29.10 12:00 -> 02.11 12:00
    assert (str(ranged.load_from), str(ranged.load_to)) == ("2026-10-29 12:00:00", "2026-11-02 12:00:00")
    st = Store(tmp_path / "w.db")
    st.save_orders("okna", df)
    back = st.load_orders("okna")
    assert back.load_from.notna().sum() == df.load_from.notna().sum() and back.load_to.iloc[0] == df.load_to.iloc[0]


def test_optimizer_orders_stops_by_windows_and_rejects_impossible():
    import datetime as _dt
    from ltl.optimizer import TimeCtx
    from ltl.schedule import ScheduleParams, order_windows
    rows = [("A", 52.5, 13.4, 48.9, 2.35, 6.0, 10, 1500), ("B", 52.4, 16.9, 45.7, 4.8, 6.0, 10, 1500)]
    df = _orders(rows)
    df["load_date"] = _dt.date(2026, 11, 2)
    df["unload_date"] = _dt.date(2026, 11, 6)
    df["load_from"], df["load_to"] = pd.Timestamp("2026-11-02 06:00"), pd.Timestamp("2026-11-02 18:00")
    df["unload_from"], df["unload_to"] = pd.Timestamp("2026-11-03 06:00"), pd.Timestamp("2026-11-10 18:00")
    p = ScheduleParams()

    def seqs(d, **kw):
        v, _ = find_best_combinations(d, CostParams(), OptimizerParams(max_detour_km=800),
                                      time_ctx=TimeCtx(p, order_windows(d, p)), **kw)
        return [[(s.order_id, s.kind) for s in x.stops] for x in v]

    assert seqs(df)[0] == [("B", "L"), ("A", "L"), ("A", "U"), ("B", "U")]            # najkrótsza kolejność mieści się w oknach
    a_first = df.copy()
    a_first["load_from"] = [pd.Timestamp("2026-11-02 06:00"), pd.Timestamp("2026-11-02 13:00")]
    a_first["load_to"] = [pd.Timestamp("2026-11-02 08:00"), pd.Timestamp("2026-11-02 18:00")]
    assert seqs(a_first)[0][:2] == [("A", "L"), ("B", "L")]                            # okna wymuszają A przed B
    b_first = df.copy()
    b_first["load_from"] = [pd.Timestamp("2026-11-02 13:00"), pd.Timestamp("2026-11-02 06:00")]
    b_first["load_to"] = [pd.Timestamp("2026-11-02 18:00"), pd.Timestamp("2026-11-02 08:00")]
    assert seqs(b_first)[0][:2] == [("B", "L"), ("A", "L")]
    both_early = df.copy()
    both_early["load_to"] = pd.Timestamp("2026-11-02 07:00")                          # dwa załadunki 300 km od siebie w godzinę
    assert seqs(both_early) == []
    tight = df.copy()
    tight["unload_to"] = [pd.Timestamp("2026-11-10 18:00"), pd.Timestamp("2026-11-03 18:00")]   # B rozładunek dzień po załadunku (>1000 km)
    assert seqs(tight) == []
    # bez okien (wyłączone w ustawieniach) ta sama para jest dozwolona
    off = TimeCtx(ScheduleParams(enabled=False), order_windows(tight, p))
    assert find_best_combinations(tight, CostParams(), OptimizerParams(max_detour_km=800), time_ctx=off)[0]
    # punkt dodatkowy trafia w miejsce, które mieści się w oknach; start pojazdu liczy się do harmonogramu
    from ltl.optimizer import Stop
    on_way = Stop("P1", "W", "DE", "50667", "Kolonia", 50.94, 6.96)
    assert any(k == "W" for _, k in sum(seqs(df, waypoints=(on_way,)), []))
    far_start = Stop("START", "S", "ES", "28001", "", 40.4, -3.7)
    assert seqs(df, origin=far_start)                                                 # start automatyczny: pojazd ruszy odpowiednio wcześniej
    fixed = ScheduleParams(start_at=pd.Timestamp("2026-11-02 06:00").to_pydatetime())  # a przy stałym starcie o 6:00 z Madrytu nie zdąży
    v, _ = find_best_combinations(df, CostParams(), OptimizerParams(max_detour_km=800), origin=far_start,
                                  time_ctx=TimeCtx(fixed, order_windows(df, fixed)))
    assert v == []


def test_idle_days_waiting_for_windows_cost_money():
    import datetime as _dt
    from ltl.optimizer import TimeCtx, Stop
    from ltl.route_edit import evaluate_route, route_for_variant
    from ltl.routing import TruckProfile
    from ltl.schedule import ScheduleParams, order_windows
    rows = [("A", 52.5, 13.4, 48.9, 2.35, 6.0, 10, 1500), ("B", 52.4, 16.9, 45.7, 4.8, 6.0, 10, 1500)]
    df = _orders(rows)
    df["load_date"] = _dt.date(2026, 11, 2)
    df["unload_date"] = _dt.date(2026, 11, 6)
    df["load_from"], df["load_to"] = pd.Timestamp("2026-11-02 06:00"), pd.Timestamp("2026-11-02 18:00")
    df["unload_from"], df["unload_to"] = pd.Timestamp("2026-11-12 06:00"), pd.Timestamp("2026-11-20 18:00")   # okno otwiera się dopiero 12.11
    for charge in (True, False):
        p = ScheduleParams(charge_idle_days=charge)
        ctx = TimeCtx(p, order_windows(df, p))
        v, _ = find_best_combinations(df, CostParams(), OptimizerParams(max_detour_km=800), time_ctx=ctx)
        r = route_for_variant(v[0], [], TruckProfile(), "none", "", "", 65.0)
        ev = evaluate_route(v[0], r, CostParams(), ctx)
        assert ev["schedule"].wait_h > 24                                   # pojazd czeka na otwarcie okna rozładunku
        if charge:
            assert ev["idle_days"] >= 1 and ev["idle_cost"] == ev["idle_days"] * 300.0
            charged, idle_cost = ev["profit"], ev["idle_cost"]
        else:
            assert ev["idle_cost"] == 0
            free = ev["profit"]
    assert charged < free and abs((free - charged) - idle_cost) < 1e-6              # różnica to dokładnie koszt dni czekania


# ----------------------------------------------------------------- workspace
def _wo(i, ldm=4.0, w=5.0, price=500.0, sp="62-800", ep="75001", lat=52.0, lon=17.0):
    return {"id": i, "client": "K", "start_country": "pl", "start_postal": sp, "start_city": "A",
            "end_country": "fr", "end_postal": ep, "end_city": "B", "start_lat": lat, "start_lon": lon,
            "end_lat": 48.85, "end_lon": 2.35, "ldm": ldm, "weight_t": w, "price_eur": price,
            "load_date": "2026-12-01", "unload_date": "2026-12-03"}


def test_workspace_plan_edit_and_validate():
    from ltl import workspace as ws
    p = ws.new_plan("t")
    assert ws.add_orders(p, [_wo("A"), _wo("B"), _wo("A")]) == ["A", "B"]       # duplikat numeru pominięty
    assert [(s["kind"], s["order_id"]) for s in p["stops"]] == [("L", "A"), ("U", "A"), ("L", "B"), ("U", "B")]
    assert ws.validate(p) == []
    ws.reorder(p, [0, 2, 1, 3])                                                  # L A, L B, U A, U B
    assert ws.validate(p) == [] and ws.peaks(p) == (8.0, 10.0)
    ws.reorder(p, [2, 0, 1, 3])                                                  # U A przed L A
    assert any("rozładunek jest przed załadunkiem" in x for x in ws.validate(p))
    p2 = ws.new_plan()
    ws.add_orders(p2, [_wo("X", ldm=9.0), _wo("Y", ldm=9.0)])
    ws.reorder(p2, [0, 2, 1, 3])
    assert any("Przeładowanie" in x for x in ws.validate(p2, max_ldm=13.6))
    assert not any("Przeładowanie" in x for x in ws.validate(p2, max_ldm=20))


def test_workspace_waypoints_and_removal():
    from ltl import workspace as ws
    p = ws.new_plan()
    ws.add_orders(p, [_wo("A"), _wo("B")])
    w1 = ws.add_waypoint(p, "de", "10115", "Berlin", 52.5, 13.4, "magazyn", position=2)
    w2 = ws.add_waypoint(p, "pl", "00-001", "", 52.2, 21.0)
    assert (w1, w2) == ("P1", "P2") and p["stops"][2]["order_id"] == "P1"
    ws.remove_stop(p, 2)                                                         # punkt dodatkowy znika sam
    assert [s["order_id"] for s in p["stops"] if s["kind"] == "W"] == ["P2"] and len(p["orders"]) == 2
    ws.remove_stop(p, 0)                                                         # załadunek A = usunięcie zlecenia A
    assert ws.order_ids(p) == ["B"] and all(s.get("order_id") != "A" for s in p["stops"])


def test_workspace_update_orders_geocodes_changes():
    from ltl import workspace as ws
    p = ws.new_plan()
    ws.add_orders(p, [_wo("A"), _wo("B")])
    calls = []
    rows = [dict(p["orders"][0], end_postal="69001"), _wo("C", ep="10115")]
    res = ws.update_orders(p, rows, geocode=lambda c, pc: (calls.append((c, pc)), (1.0, 2.0))[1])
    assert res["added"] == ["C"] and res["removed"] == ["B"] and "A" in res["relocated"]
    assert ("FR", "69001") in calls and ("FR", "10115") in calls
    assert ws.order_ids(p) == ["A", "C"]
    assert [(s["kind"], s["order_id"]) for s in p["stops"]] == [("L", "A"), ("U", "A"), ("L", "C"), ("U", "C")]


def test_workspace_variant_and_roundtrip():
    from ltl import workspace as ws
    from ltl.costs import CostParams
    from ltl.optimizer import Stop
    p = ws.new_plan()
    ws.add_orders(p, [_wo("A")])
    v = ws.to_variant(p, Stop("START", "S", "PL", "62-800", "", 52.0, 17.0), CostParams())
    assert [s.kind for s in v.stops] == ["S", "L", "U"] and v.revenue == 500.0 and v.total_km > 1000
    assert ws.to_variant(ws.new_plan(), None, CostParams()) is None
    frame = ws.orders_frame(p)
    assert frame.loc[0, "id"] == "A" and frame.loc[0, "load_date"].isoformat() == "2026-12-01"
    back = ws.from_variant(v, frame)
    assert ws.order_ids(back) == ["A"] and [s["kind"] for s in back["stops"]] == ["L", "U"]


def test_store_vehicles_and_plans_with_conflicts(tmp_path):
    from ltl.storage import NameExists, PlanConflict, Store
    s = Store(tmp_path / "w.db")
    v = s.save_vehicle(name="WX 111", start_country="pl", start_postal="62-800", max_ldm=13.6)
    s.save_vehicle(name="WX 222")
    with __import__("pytest").raises(NameExists):
        s.save_vehicle(name="WX 111")
    assert list(s.vehicles()["name"]) == ["WX 111", "WX 222"]
    # praca osobista: każdy użytkownik ma własny plan; grupowa: wspólny
    assert s.save_plan("personal", "ala", v, {"orders": [1]}, 0, "ala") == 1
    assert s.get_plan("personal", "ola", v) is None and s.get_plan("personal", "ala", v)["version"] == 1
    assert s.save_plan("group", "", v, {"orders": [1, 2]}, 0, "ala") == 1
    with __import__("pytest").raises(PlanConflict) as ei:                        # druga osoba nie zna wersji 1
        s.save_plan("group", "", v, {"orders": []}, 0, "ola")
    assert ei.value.current["updated_by"] == "ala"
    assert s.save_plan("group", "", v, {"orders": [1, 2, 3]}, 1, "ola") == 2
    assert s.plans_overview("group", "")[v]["n_orders"] == 3
    s.retire_vehicle(v)
    assert list(s.vehicles()["name"]) == ["WX 222"] and s.get_plan("group", "", v) is not None
    s.delete_vehicle(v)
    assert s.get_plan("group", "", v) is None


def test_save_vehicles_editor_diff(tmp_path):
    from ltl.storage import Store
    from ltl.workspace_ui import VEHICLE_COLUMNS, save_vehicles
    s = Store(tmp_path / "v.db")
    a = s.save_vehicle(name="A1", start_country="PL", start_postal="62-800")
    b = s.save_vehicle(name="B2")
    before = s.vehicles()
    shown = before.drop(columns=["active", "id"]).reset_index(drop=True).rename(columns=VEHICLE_COLUMNS)
    edited = shown.copy()
    edited.loc[0, VEHICLE_COLUMNS["note"]] = "zmieniony"
    edited = edited.drop(index=1)                                           # B2 usunięty = wycofany
    edited.loc[2] = ["C3", "", "firanka", 13.6, 24.0, "DE", "10115", "Berlin"]   # nowy wiersz
    edited.loc[3] = ["A1", "", "", 13.6, 24.0, "", "", ""]                  # duplikat nazwy
    msgs = save_vehicles(s, before, edited)
    assert any("już istnieje" in m for _, m in msgs) and any("nowe 1, zaktualizowane 1, wycofane 1" in m for _, m in msgs)
    names = list(s.vehicles()["name"])
    assert names == ["A1", "C3"] and s.vehicles().iloc[0]["note"] == "zmieniony"


def test_workspace_stop_edits_and_editor_roundtrip():
    from ltl import workspace as ws
    from ltl.workspace_ui import _orders_editor_frame, _rows_from_editor
    p = ws.new_plan()
    ws.add_orders(p, [_wo("A"), _wo("B"), _wo("C")])
    ws.add_waypoint(p, "de", "10115", "Berlin", 52.5, 13.4, "x")
    # przestawienie: C na początek (numer 0), usunięcie punktu dodatkowego i zlecenia B
    nums = [2, 3, 4, 5, 0, 0.5, 6]
    dels = [False, False, True, False, False, False, True]
    dropped = ws.apply_stop_edits(p, nums, dels, [None] * 7)
    assert dropped == {"B"} and ws.order_ids(p) == ["A", "C"]
    assert [(s["kind"], s["order_id"]) for s in p["stops"]] == [("L", "C"), ("U", "C"), ("L", "A"), ("U", "A")]
    # tabela zleceń: edycja wartości, nowy wiersz bez numeru, usunięty wiersz
    df = _orders_editor_frame(p)
    assert list(df["Nr"]) == ["A", "C"]
    df.loc[0, "Cena (EUR)"] = 999.0
    df.loc[2] = [None, "Nowy", "PL", "00-001", "Warszawa", None, "DE", "10115", "Berlin", None, 3.0, 4.0, 700.0, ""]
    df = df.drop(index=1)                                                        # zlecenie C usunięte
    rows = _rows_from_editor(p, df)
    res = ws.update_orders(p, rows, geocode=lambda c, pc: (50.0, 10.0))
    assert res["added"] == ["M1"] and res["removed"] == ["C"] and ws.order_ids(p) == ["A", "M1"]
    assert p["orders"][0]["price_eur"] == 999.0 and p["orders"][0]["start_lat"] == 52.0     # istniejące współrzędne zachowane
    assert p["orders"][1]["start_lat"] == 50.0 and p["orders"][1]["client"] == "Nowy"


def test_workspace_live_edit_detection_and_blank_location():
    from ltl import workspace as ws
    p = ws.new_plan()
    ws.add_orders(p, [_wo("A"), _wo("B")])
    same = [dict(o) for o in p["orders"]]
    assert not ws.orders_differ(p, same)                                         # brak zmian = brak zapisu
    assert ws.orders_differ(p, [dict(same[0], price_eur=1.0), same[1]])           # zmiana ceny
    assert ws.orders_differ(p, same[:1])                                          # usunięty wiersz
    partial = {"id": "N", "start_country": "PL", "start_postal": "00-001"}        # niepełny nowy wiersz: jeszcze nie liczymy
    assert not ws.orders_differ(p, same + [partial])
    full = {**_wo("N"), "start_lat": None, "end_lat": None}
    assert ws.orders_differ(p, same + [full])
    # kasowanie kodu czyści współrzędne (stara lokalizacja nie może zostać)
    res = ws.update_orders(p, [dict(same[0], end_postal=""), same[1]], geocode=lambda c, pc: (1.0, 2.0))
    assert p["orders"][0]["end_lat"] is None and any("nie ustalono położenia" in x for x in ws.validate(p))
    # edycje kolejności
    q = ws.new_plan()
    ws.add_orders(q, [_wo("A"), _wo("B")])
    assert not ws.stop_edits_pending(q, [1, 2, 3, 4], [False] * 4, [None] * 4)
    assert ws.stop_edits_pending(q, [1, 3, 2, 4], [False] * 4, [None] * 4)
    assert ws.stop_edits_pending(q, [1, 2, 3, 4], [False, False, True, False], [None] * 4)


# ------------------------------------------------- dopisywanie ofert i wczytywanie eksportu historii
def test_history_export_csv_roundtrip(tmp_path):
    from ltl.ingest import parse_sources_report, read_source
    from ltl.sample import SAMPLE_TEXT
    from ltl.storage import Store
    orders, _ = parse_sources_report([("p", SAMPLE_TEXT)])
    st_ = Store(tmp_path / "h.db")
    st_.record_offers(orders)
    exported = st_.query_history(limit=1000).to_csv(index=False, sep=";").encode("utf-8-sig")   # jak „Pobierz CSV”
    frame = read_source("historia_ofert.csv", exported)
    assert not isinstance(frame, str) and len(frame) == len(orders)
    back, rep = parse_sources_report([("historia_ofert.csv", frame)])
    assert len(back) == len(orders) and rep[0]["found"] == len(orders)
    key = ["start_country", "start_postal", "end_country", "end_postal", "ldm", "weight_t"]
    assert sorted(map(tuple, back[key].astype(str).values)) == sorted(map(tuple, orders[key].astype(str).values))
    assert (back["vehicles"] == orders["vehicles"]).all() or set(back["vehicles"]) <= set(orders["vehicles"]) | {""}
    # zwykły plik z nagłówkami po polsku nadal idzie przez parser tekstowy
    assert isinstance(read_source("o.csv", "Załadunek;Rozładunek;Data;Waga;LDM;Cena\nDE 20095;PL 00-001;2026-12-01;5;4;900\n".encode()), str)


def test_append_orders_keeps_edits_dedupes_and_numbers():
    from ltl.ingest import append_orders, parse_sources_report
    from ltl.sample import SAMPLE_TEXT
    old, _ = parse_sources_report([("p", SAMPLE_TEXT)])
    old = old.copy()
    old.loc[0, "price_eur"] = 1234.0                              # ręczna poprawka musi zostać
    n = len(old)
    combined, st_ = append_orders(old, old.copy())                # te same oferty drugi raz = wszystkie powtórzenia
    assert len(combined) == n and st_ == {"added": 0, "duplicates": n} and combined.loc[0, "price_eur"] == 1234.0
    extra = old.iloc[:2].copy()
    extra["start_postal"] = ["99999", "88888"]                    # naprawdę nowe oferty
    combined, st_ = append_orders(old, extra)
    assert len(combined) == n + 2 and st_["added"] == 2
    assert combined["id"].is_unique and list(combined["id"][:n]) == list(old["id"])
    assert all(i[0] == old["id"].iloc[0][0] for i in combined["id"][n:])        # kolejne numery tego samego źródła
    only_new, st2 = append_orders(None, extra)                    # brak starych = po prostu nowe
    assert len(only_new) == 2 and st2["added"] == 2


def test_geo_duplicates_same_map_point():
    import pandas as pd
    from ltl.duplicates import DupParams, find_geo_duplicates

    def row(i, sp, slat, slon, ep, elat, elon, ldm=4.0, w=5.0, geo="online", date="2026-12-01", price=500.0):
        return {"id": i, "start_country": "FR", "start_postal": sp, "end_country": "DE", "end_postal": ep,
                "start_lat": slat, "start_lon": slon, "end_lat": elat, "end_lon": elon, "weight_t": w, "ldm": ldm,
                "load_date": date, "start_geo": geo, "end_geo": geo, "price_eur": price}
    df = pd.DataFrame([
        row("A", "75001", 48.860, 2.340, "10115", 52.53, 13.38, price=400),
        row("B", "75002", 48.865, 2.343, "10117", 52.52, 13.39, price=900),      # ten sam punkt (<3 km), inne kody
        row("C", "69001", 45.76, 4.83, "10115", 52.53, 13.38),                    # inny punkt załadunku
        row("D", "75003", 48.862, 2.341, "10115", 52.53, 13.38, ldm=9.0),         # inny LDM = inny ładunek
        row("E", "75004", 48.861, 2.342, "10115", 52.53, 13.38, geo="kraj"),      # nie zlokalizowano - środek kraju
    ])
    prio = pd.Series([1.0, 2.0, 1.0, 1.0, 1.0], index=df.index)
    dup = find_geo_duplicates(df, prio, DupParams(geo_radius_km=3.0))
    assert dup == {0: "B"}                                                        # zostaje droższa oferta B
    assert find_geo_duplicates(df, prio, DupParams(geo_radius_km=0.0)) == {}      # wyłączone
    far = df.copy(); far.loc[1, ["start_lat", "start_lon"]] = [49.5, 2.3]         # >3 km od A
    assert find_geo_duplicates(far, prio, DupParams(geo_radius_km=3.0)) == {}
    other_date = df.copy(); other_date.loc[1, "load_date"] = "2026-12-05"
    assert find_geo_duplicates(other_date, prio, DupParams(geo_radius_km=3.0)) == {}


# ------------------------------------------------------------ geokodowanie kodów pocztowych (odporność)
def test_postal_candidates_cover_country_formats():
    from ltl.geo import postal_candidates as pc
    assert pc("NL", "4611 AA") == ["4611AA", "4611"] and pc("NL", "4611") == ["4611"]
    assert pc("PL", "62800")[0] == "62-800" and "62800" in pc("PL", "62800") and pc("PL", " 62 800")[0] == "62-800"
    assert pc("CZ", "11000")[0] == "110 00" and "11000" in pc("CZ", "11000")
    assert pc("GB", "NN11 4") == ["NN11"] and pc("GB", "SW1A 1AA") == ["SW1A"] and pc("GB", "G1 1") == ["G1"]
    assert pc("FR", "FR-75001") == ["75001"] and pc("LU", "L-1234") == ["1234"] and pc("IE", "D02 X285") == ["D02"]
    assert pc("PT", "4000-001") == ["4000-001", "4000"]


class _Resp:
    def __init__(self, body):
        self.b = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, *a):
        return self.b


def _fake_net(monkeypatch, zip_known=None, mode=None, log=None):
    import json
    import urllib.error
    import urllib.request
    zip_known = zip_known or {}
    mode = mode if mode is not None else {"zip": "ok", "nom": "ok"}

    def fake(req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        (log if log is not None else []).append(url)
        if "zippopotam" in url:
            if mode["zip"] == "down":
                raise urllib.error.URLError("timed out")
            code = urllib.request.unquote(url.rsplit("/", 1)[1])
            if code in zip_known:
                lat, lon = zip_known[code]
                return _Resp(json.dumps({"places": [{"latitude": str(lat), "longitude": str(lon)}]}).encode())
            raise urllib.error.HTTPError(url, 404, "nf", {}, None)
        if mode["nom"] == "down":
            raise urllib.error.URLError("down")
        return _Resp(b"[]")
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return mode


def test_geocoder_tries_code_variants(monkeypatch):
    from ltl.geo import Geocoder
    _fake_net(monkeypatch, zip_known={"4611": (51.49, 4.29), "110 00": (50.08, 14.43)})
    g = Geocoder(online=True, cache_path=None)
    assert g.locate("NL", "4611AA")[2] == "online"          # zippopotam zna tylko 4 cyfry
    assert g.locate("CZ", "11000")[2] == "online"           # wersja ze spacją
    assert g.locate("NL", "4611 AA")[2] == "cache"          # inny zapis tego samego kodu = ten sam wpis


def test_geocoder_network_errors_are_not_permanent(monkeypatch):
    from ltl.geo import Geocoder
    mode = _fake_net(monkeypatch, zip_known={"75001": (48.86, 2.34)})
    g = Geocoder(online=True, cache_path=None)
    g.locate("FR", "99999")                                  # jedna awaria sieci nie wyłącza usługi
    mode["zip"] = "down"
    g.locate("FR", "88888")
    assert g.online and g._service_ok("zippopotam")
    mode["zip"] = "ok"
    assert g.locate("FR", "75001")[2] == "online"
    mode["zip"] = "down"                                     # seria błędów wstrzymuje usługę, ale tylko na chwilę
    for c in ("11111", "22222", "33333"):
        g.locate("FR", c)
    assert not g._service_ok("zippopotam")
    g.reset_failures()
    assert g._service_ok("zippopotam")


def test_geocoder_prefix_guess_miss_cache_manual_and_legacy(monkeypatch):
    from ltl.geo import Geocoder
    log = []
    _fake_net(monkeypatch, zip_known={}, log=log)
    g = Geocoder(online=True, cache_path=None)
    g._cache["FR|75001"] = [48.86, 2.34]
    g._cache["FR|75008"] = [48.87, 2.31]
    lat, lon, src = g.locate("FR", "75099")                  # nieznany, ale podobny do znanych (75…)
    assert src == "prefiks" and 48.8 < lat < 48.9
    assert g.locate("FR", "99999")[2] == "kraj"              # nic podobnego = środek kraju
    n = len(log)
    g.locate("FR", "99999")
    assert len(log) == n                                     # nieznany kod pamiętany: bez ponownego pytania sieci
    g.set_manual("FR", "99999", 45.0, 3.0)
    assert g.locate("FR", "99999")[:3] == (45.0, 3.0, "cache")
    g._cache["CZ|11000"] = [50.0, 14.0]                      # klucz ze starej wersji (kod bez spacji)
    assert g.locate("CZ", "110 00")[2] == "cache"
    off = Geocoder(online=False, cache_path=None)
    assert off.locate("DE", "20095")[2] == "kraj" and not off.nominatim_ok


def test_geocoder_nominatim_city_fallback(monkeypatch):
    from ltl.geo import Geocoder
    g = Geocoder(online=True, cache_path=None)
    monkeypatch.setattr(g, "_fetch", lambda c, code: None)
    monkeypatch.setattr(g, "_fetch_nominatim", lambda c, p: None)
    monkeypatch.setattr(g, "_fetch_nominatim_city", lambda c, p, city: (52.0, 21.0) if city == "Warszawa" else None)
    assert g.locate("PL", "00-001", "Warszawa")[2] == "online"
    assert g.locate("PL", "00-002", "")[2] == "prefiks"       # bez miasta, ale podobny do znanego kodu
    assert g.locate("DE", "20095", "")[2] == "kraj"


# ------------------------------------------- optymalizacja wydajności: wyniki muszą być identyczne jak we wzorcach
def _synthetic_orders(n, seed):
    import datetime as dt
    import numpy as np
    rng = np.random.default_rng(seed)
    base = dt.date(2026, 12, 1)
    rows = []
    for i in range(n):
        a = rng.uniform([44.0, 0.0], [54.0, 18.0])
        b = a + rng.uniform([-4.0, -5.0], [4.0, 6.0])
        rows.append({"id": f"T{i + 1}", "start_country": "FR", "start_postal": f"{10000 + i}", "start_city": "A",
                     "end_country": "DE", "end_postal": f"{20000 + i}", "end_city": "B",
                     "start_lat": a[0], "start_lon": a[1], "end_lat": b[0], "end_lon": b[1],
                     "ldm": float(rng.choice([0.5, 1.2, 2.0, 3.5, 5.0, 8.0, 13.6])), "weight_t": float(rng.choice([0.5, 2.0, 6.0, 12.0, 24.0])),
                     "price_eur": float(rng.integers(200, 2500)), "load_date": base + dt.timedelta(days=int(rng.integers(0, 4)))})
    return pd.DataFrame(rows)


def test_optimizer_matches_reference_implementation():
    import ref_optimizer as old
    from ltl import optimizer as new
    from ltl.optimizer import OptimizerParams, Stop
    origin = Stop("START", "S", "PL", "62-800", "", 52.0, 17.0)
    wp = (Stop("P1", "W", "DE", "10115", "Berlin", 52.52, 13.4),)

    def sig(vs):
        return [(tuple(v.order_ids), round(v.profit, 6), round(v.total_km, 6), [(s.kind, s.order_id) for s in v.stops]) for v in vs]
    for seed, kw, prm in ((1, {}, {}), (2, {"origin": origin}, {"max_detour_km": 150.0}),
                          (3, {"origin": origin, "waypoints": wp}, {"hard_cap_orders": 4}),
                          (4, {}, {"max_load_date_gap_days": None})):
        df = _synthetic_orders(60, seed)
        a, sa = old.find_best_combinations(df, CostParams(), OptimizerParams(top_n=15, **prm), **kw)
        b, sb = new.find_best_combinations(df, CostParams(), OptimizerParams(top_n=15, **prm), **kw)
        assert a and sig(a) == sig(b) and sa == sb, f"różnice dla ziarna {seed}"


def test_best_route_and_insert_match_reference_on_random_inputs():
    import random
    import numpy as np
    import ref_optimizer as old
    from ltl import optimizer as new
    from ltl.geo import road_distance_matrix
    random.seed(11)
    np.random.seed(11)
    for _ in range(600):
        n = random.randint(2, 8)
        D = road_distance_matrix(np.random.uniform(40, 55, 2 * n), np.random.uniform(-5, 20, 2 * n))
        ldm = np.random.choice([0.5, 2, 4, 6, 8, 10, 13.6], n).astype(float)
        wt = np.random.choice([1, 5, 12, 20, 24], n).astype(float)
        mx_l, mx_w = random.choice([13.6, 10.0]), random.choice([24.0, 15.0])
        d0 = None if random.random() < 0.5 else np.random.uniform(0, 500, 2 * n)
        forb = random.randint(0, 2 * n - 1)
        ok = None if random.random() < 0.6 else (lambda seq, f=forb: seq[-1] != f)
        idx = random.sample(range(n), min(random.choice([1, 2, 2, 3]), n))
        assert old._best_route(idx, D, ldm, wt, mx_l, mx_w, d0=d0, ok=ok) == new._best_route(idx, D, ldm, wt, mx_l, mx_w, d0=d0, ok=ok)
        base = random.sample(range(n), random.randint(1, n - 1))
        j = random.choice([x for x in range(n) if x not in base])
        r = old._best_route(sorted(base), D, ldm, wt, mx_l, mx_w)
        if r:
            assert old._insert(r[1], j, D, ldm, wt, mx_l, mx_w, ok) == new._insert(r[1], j, D.tolist(), ldm, wt, mx_l, mx_w, ok)


def test_merge_edits_matches_reference_implementation():
    from ltl.ingest import parse_sources_report
    import random
    import numpy as np
    import ref_ordertable as old
    from ltl.ordertable import merge_edits
    from ltl.sample import SAMPLE_TEXT
    work, _ = parse_sources_report([("p", SAMPLE_TEXT)])
    random.seed(5)
    for trial in range(40):
        ids = list(work["id"])
        view = random.sample(ids, random.randint(2, len(ids)))
        ed = work[work["id"].isin(view)].copy()
        for i in random.sample(list(ed.index), min(3, len(ed))):
            ed.loc[i, "price_eur"] = random.choice([100.0, np.nan, 555.5])
        ed = ed.drop(index=random.sample(list(ed.index), min(2, max(len(ed) - 1, 0))))      # zawsze coś zostaje
        for k in range(random.randint(0, 3)):
            r = ed.iloc[0].copy()
            r["id"] = None if k % 2 == 0 else f"X{k}{trial}"
            ed = pd.concat([ed, r.to_frame().T], ignore_index=True)
        if trial % 7 == 0 and len(ed):
            ed = pd.concat([ed, ed.iloc[[0]]], ignore_index=True)
        pd.testing.assert_frame_equal(old.merge_edits(work, view, ed).reset_index(drop=True),
                                      merge_edits(work, view, ed).reset_index(drop=True), check_dtype=False)


def test_sync_windows_and_duplicates_match_reference():
    import datetime as dt
    import random
    import ref_duplicates as dold
    import ref_schedule as sold
    from ltl.duplicates import DupParams, find_duplicates, find_geo_duplicates
    from ltl.schedule import sync_windows
    random.seed(3)
    base = dt.date(2026, 12, 1)
    for _ in range(80):
        rows = []
        for _i in range(random.randint(1, 25)):
            r = {}
            for side in ("load", "unload"):
                f = random.choice([pd.NaT, pd.Timestamp(base) + pd.Timedelta(days=random.randint(0, 4), hours=random.randint(0, 20))])
                r[f"{side}_date"] = random.choice([None, base + dt.timedelta(days=random.randint(0, 4))])
                r[f"{side}_from"] = f
                r[f"{side}_to"] = f + pd.Timedelta(hours=3) if f is not pd.NaT else pd.NaT
            rows.append(r)
        df = pd.DataFrame(rows)
        for c in ("load_from", "load_to", "unload_from", "unload_to"):
            df[c] = pd.to_datetime(df[c])
        pd.testing.assert_frame_equal(sold.sync_windows(df), sync_windows(df), check_dtype=False)
    from ltl.ingest import parse_sources_report
    from ltl.sample import SAMPLE_TEXT
    df, _ = parse_sources_report([("p", SAMPLE_TEXT)])
    extra = df.copy()
    extra["id"] = ["Z" + i for i in extra["id"]]
    extra["start_postal"] = extra["start_postal"].astype(str).str[:3] + "99"
    both = pd.concat([df, extra], ignore_index=True)
    prio = pd.Series(range(len(both)), index=both.index, dtype=float)
    for prm in (DupParams(), DupParams(tolerance_pct=0.0), DupParams(prefix_len=4)):
        assert dold.find_duplicates(both, prio, prm) == find_duplicates(both, prio, prm)
    g = both.assign(start_lat=48.0 + (both.index % 5) * 0.01, start_lon=2.0, end_lat=52.0, end_lon=13.0, start_geo="cache", end_geo="cache")
    for r in (3.0, 30.0):
        prm = DupParams(geo_radius_km=r)
        assert dold.find_geo_duplicates(g, prio, prm) == find_geo_duplicates(g, prio, prm)


def test_route_cache_is_bounded_and_dedupes_parallel_requests():
    import threading
    import time as _t
    from ltl import routing
    c = routing.LRUCache(3)
    for i in range(6):
        c[i] = i
    assert len(c) == 3 and 0 not in c and c.get(5) == 5
    calls = []

    def slow():
        calls.append(1)
        _t.sleep(0.15)
        return "leg"
    routing._LEG_CACHE.clear()
    out = []
    ts = [threading.Thread(target=lambda: out.append(routing._cached(("k", 1), slow))) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert out == ["leg"] * 6 and len(calls) == 1            # sześć wątków, jedno zapytanie
    routing._LEG_CACHE.clear()


# ------------------------------------------------------------ formaty ustawień
def test_settings_sanitize_snapshot_and_store(tmp_path):
    from ltl import settings as cfg
    from ltl.storage import Store
    d = cfg.sanitize({"s_fuel": 99.0, "s_cons": "abc", "s_gap": 2.6, "s_win": "tak", "s_provider": "Nieznany",
                      "s_bodies": ["firanka", "kosmiczny"], "bzdura": 1, "origin_country": "DE",
                      cfg.RULES_KEY: [{"Kraj": "CH"}]}, ("firanka",), ("firanka", "mega"))
    assert d["s_fuel"] == 4.0                                   # poza zakresem -> przycięte
    assert d["s_cons"] == 27.0 and d["s_win"] is True           # błędny typ -> domyślne
    assert d["s_gap"] == 3 and isinstance(d["s_gap"], int)       # liczby całkowite zostają całkowite
    assert d["s_provider"] == cfg.PROVIDERS[0] and d["s_bodies"] == ["firanka"] and "bzdura" not in d
    assert d["origin_country"] == "DE" and d[cfg.RULES_KEY] == [{"Kraj": "CH"}]
    snap = cfg.snapshot({"s_fuel": 1.8, "inne": 5}, [{"a": 1}])
    assert snap == {"s_fuel": 1.8, cfg.RULES_KEY: [{"a": 1}]}   # tylko znane klucze
    st_ = Store(tmp_path / "f.db")
    assert st_.list_formats() == [] and st_.get_format("Ania") is None
    st_.save_format("Ania", snap)
    st_.save_format("andrzej", {"s_fuel": 2.0})
    st_.save_format("Ania", {"s_fuel": 1.9})                    # nadpisanie tej samej nazwy
    assert st_.list_formats() == ["andrzej", "Ania"] and st_.get_format("Ania") == {"s_fuel": 1.9}
    st_.delete_format("Ania")
    assert st_.list_formats() == ["andrzej"]


# ---------------------------------------------------------------- trasy, warianty i tabela w układzie arkusza
def _sheet_plan():
    from ltl import workspace as w
    p = w.new_plan()
    for i, (a, b, price, ld, ud) in enumerate([("GB", "DE", 1500, "2026-11-02", "2026-11-04"),
                                               ("GB", "NL", 1000, "2026-11-03", "2026-11-05")]):
        p["orders"].append(w.clean_order({"id": f"T{i}", "client": "abc", "start_country": a, "start_postal": "E1",
                                          "end_country": b, "end_postal": "1000", "ldm": 5, "weight_t": 8, "price_eur": price,
                                          "distance_km": 600 - 200 * i, "load_date": ld, "unload_date": ud, "pay_term_days": 30}))
        p["stops"] += [{"kind": "L", "order_id": f"T{i}"}, {"kind": "U", "order_id": f"T{i}"}]
    return p


def test_sheet_rows_sums_and_margins():
    from ltl import route_sheet as rs
    plan = _sheet_plan()
    ev = {"total": 1200.0, "profit": 1300.0, "fees": 100.0, "fuel": 500.0, "tolls": 300.0}
    route, orders, end = rs.build_sheet("TR-2026/11-0001", plan, ev, 1100, 5, {"place": "PL · Poznań", "date": None},
                                        lambda c, p: "Londyn")
    assert [o["Nr trasy/zlecenia"] for o in orders] == ["TR-2026/11-0001-01", "TR-2026/11-0001-02"]
    assert route["Frachty (€)"] == 2500 and route["LDM"] == 10 and route["Skąd"] == "PL · Poznań"
    assert route["Dokąd"] == "NL · Londyn" and end["date"].isoformat() == "2026-11-05"
    assert route["Koszt dzienny (€)"] == 240.0 and route["Stawka €/km"] == round(2500 / 1100, 2)
    assert sum(o["Marża łącznie (€)"] for o in orders) == route["Marża łącznie (€)"] == 1300.0     # koszty rozdzielone bez reszty
    assert orders[0]["Płatność do"] == "04.12.2026" and orders[0]["Termin płatności"] == "30 dni"
    assert orders[0]["Skąd"] == "GB · Londyn" and orders[0]["Koszt razem (€)"] is None
    assert not {"Status", "Tagi", "Odcinki", "Pojazd (zestaw)", "Kierowca"} & set(rs.COLUMNS) and "Marża łącznie (€)" in rs.COLUMNS


def test_sheet_edits_roundtrip_and_paid_sections():
    import copy
    from ltl import route_sheet as rs
    plan = _sheet_plan()
    route, orders, _ = rs.build_sheet("TR-1", plan, None, None, None, None)
    df = rs.sheet_frame(orders)
    assert not rs.apply_sheet_edits(copy.deepcopy(plan), df)                      # bez zmian = bez zapisu
    df.loc[0, ["Frachty (€)", "Płatność status", "Termin płatności", "Dyspozytor"]] = [1700, "opłacone", "45 dni", "Ala"]
    df.loc[1, "Płatność status"] = "nieznany"                                       # spoza listy = bez zmiany
    assert rs.apply_sheet_edits(plan, df)
    o = plan["orders"][0]
    assert (o["price_eur"], o["pay_status"], o["pay_term_days"], o["dispatcher"]) == (1700, "opłacone", 45, "Ala")
    assert plan["orders"][1]["pay_status"] == "nieopłacone"
    paid = rs.clean_paid_sections([{"kind": None, "name": "Dover-Calais", "cost_eur": 120}, {"kind": "Pociąg", "name": "", "cost_eur": None},
                                   {"kind": None, "name": None, "cost_eur": None}])
    assert paid == [{"kind": "Prom", "name": "Dover-Calais", "cost_eur": 120.0}, {"kind": "Pociąg", "name": "", "cost_eur": 0.0}]
    plan["paid_sections"] = paid
    assert rs.paid_total(plan) == 120.0


def test_store_routes_variants_numbering_and_migration(tmp_path):
    import sqlite3
    from ltl.storage import PlanConflict, Store
    path = str(tmp_path / "r.db")
    s = Store(path)
    v = s.save_vehicle(name="A")
    s.save_plan("group", "", v, {"orders": [{"id": "T1"}], "stops": []}, 0, "ala")
    with sqlite3.connect(path) as c:
        c.execute("DELETE FROM meta")                                               # baza sprzed migracji
    s = Store(path)
    [r1] = s.list_routes("group", "", v)
    assert r1["code"].endswith("-0001") and s.list_variants(r1["id"])[0]["n_orders"] == 1
    r2, var2 = s.create_route("group", "", v)
    assert s.list_routes("group", "", v)[-1]["code"].endswith("-0002")
    v2 = s.add_variant(r2, label="B")
    assert [x["label"] for x in s.list_variants(r2)] == ["Wariant 1", "B"]
    assert s.delete_variant(v2) and not s.delete_variant(var2)                       # ostatniego wariantu nie usuniemy
    assert s.save_variant(var2, {"orders": [1, 2]}, 1, "x") == 2
    with pytest.raises(PlanConflict):
        s.save_variant(var2, {}, 1, "y")
    s.move_route(r2, -1)
    assert [r["id"] for r in s.list_routes("group", "", v)] == [r2, r1["id"]]
    assert s.routes_overview("group", "")[v]["n_routes"] == 2
    s.delete_vehicle(v)
    assert s.list_routes("group", "", v) == []


def test_geocoder_place_name_uses_zippopotam_then_caches(tmp_path, monkeypatch):
    import io, json as _json
    from ltl import geo
    calls = []

    class Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(url, timeout=0):
        calls.append(url)
        return Resp(_json.dumps({"places": [{"place name": "Berlin", "latitude": "52.5", "longitude": "13.4"}]}).encode())

    monkeypatch.setattr(geo.urllib.request, "urlopen", fake_urlopen)
    g = geo.Geocoder(online=True, cache_path=tmp_path / "gc.json")
    assert g.place_name("DE", "10115") == "Berlin" and len(calls) == 1
    assert g.place_name("DE", "10115") == "Berlin" and len(calls) == 1            # drugi raz z cache
    g.save()
    assert geo.Geocoder(online=False, cache_path=tmp_path / "gc.json").place_name("DE", "10115") == "Berlin"
    assert geo.Geocoder(online=False, cache_path=None).place_name("FR", "75001") == ""   # offline bez cache: puste
