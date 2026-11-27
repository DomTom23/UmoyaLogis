"""Ustawienia użytkownika zapisywane jako nazwane „formaty” (np. „Andrzej”, „Ania”).

Każde ustawienie z panelu bocznego ma stały klucz w `st.session_state` (prefiks ``s_``). Format to słownik
klucz -> wartość zapisany w bazie; zmiana ustawienia zapisuje się w aktywnym formacie od razu, a wybór innego formatu
wczytuje jego wartości. Hasła i klucze API nie są zapisywane.
"""
from __future__ import annotations

DEFAULT_FORMAT = "Domyślny"
RULES_KEY = "s_rules"          # reguły cen szacunkowych (lista słowników z edytora tabeli)

# klucz: (wartość domyślna, rodzaj, min, maks)    rodzaj: "num" | "bool" | "text"
SPEC: dict[str, tuple] = {
    "s_fuel": (1.70, "num", 0.5, 4.0), "s_cons": (27.0, "num", 10.0, 60.0), "s_toll": (0.20, "num", 0.0, 1.0),
    "s_day": (300.0, "num", 0.0, 1000.0), "s_speed": (65.0, "num", 40.0, 90.0), "s_pln": (4.25, "num", 3.0, 6.0),
    "s_wph": (0.0, "num", 0.0, 24.0), "s_hand": (2.0, "num", 0.0, 8.0), "s_chan": (0.0, "num", 0.0, 2000.0),
    "s_tun": (0.0, "num", 0.0, 1000.0),
    "s_detour": (300, "num", 0, 1000), "s_gap": (3, "num", 0, 14), "s_maxord": (6, "num", 2, 12),
    "s_topn": (10, "num", 3, 20), "s_ncheck": (20, "num", 10, 40),
    "s_win": (True, "bool"), "s_open": (6, "num", 0, 23), "s_close": (18, "num", 1, 24), "s_tol": (0.0, "num", 0.0, 24.0),
    "s_drest": (11.0, "num", 0.0, 24.0), "s_wrest": (45.0, "num", 0.0, 96.0), "s_idle": (True, "bool"),
    "s_semi": (True, "bool"), "s_adr": (False, "bool"), "s_lift": (False, "bool"), "s_cab": (True, "bool"),
    "s_ftl": (True, "bool"),
    "s_dup": (True, "bool"), "s_dtol": (5.0, "num", 0.0, 50.0), "s_dpre": (3, "num", 2, 5), "s_dgeo": (3.0, "num", 0.0, 25.0),
    "s_est": (True, "bool"), "s_avoidch": (True, "bool"),
    "s_gvw": (40.0, "num", 3.5, 60.0), "s_axle": (11.5, "num", 3.0, 13.0), "s_h": (4.0, "num", 2.0, 4.5),
    "s_w": (2.55, "num", 2.0, 2.6), "s_len": (16.5, "num", 6.0, 18.75), "s_hz": (False, "bool"),
    # ustawienia z zakładki „Dane” i „Start”
    "opt_online": (True, "bool"), "opt_drop": (False, "bool"), "opt_history": (True, "bool"),
    "origin_country": ("PL", "text"), "origin_postal": ("", "text"),
}
PROVIDERS = ["Valhalla - ciężarówka (bez klucza)", "OpenRouteService - HGV (klucz API)", "Brak (linie proste)"]
LISTS = {"s_provider": PROVIDERS}      # wartości z listy (selectbox) - zapisujemy tylko dozwolone
SETTING_KEYS = tuple(SPEC) + ("s_provider", "s_bodies")


def defaults(default_bodies: tuple = ()) -> dict:
    d = {k: v[0] for k, v in SPEC.items()}
    d["s_provider"] = PROVIDERS[0]
    d["s_bodies"] = list(default_bodies)
    return d


def sanitize(data: dict | None, default_bodies: tuple = (), all_bodies: tuple = ()) -> dict:
    """Wartości z bazy -> bezpieczne dla widgetów (typ i zakres), brakujące lub błędne = domyślne."""
    out = defaults(default_bodies)
    for k, v in (data or {}).items():
        if k in SPEC:
            default, kind = SPEC[k][0], SPEC[k][1]
            if kind == "bool":
                if isinstance(v, bool):
                    out[k] = v
            elif kind == "text":
                if isinstance(v, str):
                    out[k] = v[:40]
            elif isinstance(v, (int, float)) and not isinstance(v, bool):
                lo, hi = SPEC[k][2], SPEC[k][3]
                v = min(max(v, lo), hi)
                out[k] = int(round(v)) if isinstance(default, int) else float(v)
        elif k == "s_provider" and v in PROVIDERS:
            out[k] = v
        elif k == "s_bodies" and isinstance(v, list):
            out[k] = [b for b in v if not all_bodies or b in all_bodies]
    rules = (data or {}).get(RULES_KEY)
    if isinstance(rules, list) and all(isinstance(r, dict) for r in rules):
        out[RULES_KEY] = rules
    return out


def snapshot(state, rules_records: list | None, ) -> dict:
    """Bieżące ustawienia z `st.session_state` (+ reguły cen) jako słownik do zapisu."""
    snap = {k: state[k] for k in SETTING_KEYS if k in state}
    if rules_records is not None:
        snap[RULES_KEY] = rules_records
    return snap
