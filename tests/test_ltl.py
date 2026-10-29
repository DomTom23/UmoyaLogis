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
    assert out.price_used_eur.fillna(-1).tolist() == [600, -1, 500, -1, 600, -1, 600, -1, -1, 900, 1000]
    assert out.price_kind.tolist() == ["szacunek", "brak", "szacunek", "brak", "szacunek", "brak", "szacunek",
                                       "brak", "brak", "oferta", "szacunek"]   # GB->CH: wyższa stawka (250 > 150)
    assert "poniżej minimum" in out.price_note.iloc[1] and "pasującej reguły" in out.price_note.iloc[8]
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
