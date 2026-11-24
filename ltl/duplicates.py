"""Duplikaty ofert: to samo zlecenie wystawione kilka razy (przez różnych spedytorów, z różnymi nazwami miejscowości).

Dwie oferty to duplikat, gdy jednocześnie:
* waga i LDM są identyczne lub bardzo zbliżone (tolerancja procentowa, z małym minimum bezwzględnym),
* daty załadunku się nie wykluczają (równe albo brak daty w którejś z ofert),
* odbiór i zrzut pasują do siebie: kod pocztowy (z krajem) odbioru jest taki sam jak w drugiej ofercie - nazwa
  miejscowości nie ma znaczenia - i kod zrzutu jest taki sam, ALBO jeden z końców jest taki sam, a drugi ma
  PODOBNY kod pocztowy (ten sam kraj i wspólny początek kodu).
Z grupy duplikatów zostaje oferta najlepsza: najpierw kwalifikująca się do naszego zestawu, potem z najwyższą ceną.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class DupParams:
    enabled: bool = True
    tolerance_pct: float = 5.0      # waga i LDM mogą się różnić o tyle procent (większej z wartości)
    prefix_len: int = 3             # „podobny kod” = ten sam kraj i tyle samo pierwszych znaków kodu
    ldm_abs: float = 0.2            # minimalna dopuszczalna różnica LDM (żeby małe ładunki nie były zbyt ostre)
    weight_abs_t: float = 0.1       # minimalna dopuszczalna różnica wagi w tonach


def _norm(postal) -> str:
    return "".join(ch for ch in str(postal if postal == postal and postal is not None else "").upper() if ch.isalnum())


def norm_postal(country, postal) -> str:
    """Kod do porównań: tylko litery i cyfry; w GB sam „outward code” nie wystarcza do równości, więc zostaje pełny kod."""
    return _norm(postal)


def prefix(country, postal, n: int) -> str:
    """Początek kodu do szukania podobnych: w GB część przed spacją (np. WF5), gdzie indziej pierwsze n znaków."""
    if str(country).upper() == "GB":
        head = str(postal or "").strip().upper().split(" ")[0]
        return _norm(head)
    return _norm(postal)[:n]


def same_place(c1, p1, c2, p2) -> bool:
    n1, n2 = norm_postal(c1, p1), norm_postal(c2, p2)
    return str(c1).upper() == str(c2).upper() and n1 != "" and n1 == n2


def similar_place(c1, p1, c2, p2, n: int) -> bool:
    """Ten sam kraj i wspólny początek kodu (kody krótsze niż n znaków muszą być identyczne)."""
    if str(c1).upper() != str(c2).upper():
        return False
    a, b = _norm(p1), _norm(p2)
    if not a or not b:
        return False
    if str(c1).upper() == "GB":
        return prefix(c1, p1, n) == prefix(c2, p2, n)
    if len(a) < n or len(b) < n:
        return a == b
    return a[:n] == b[:n]


def _close(a, b, tol_pct: float, abs_tol: float) -> bool:
    if pd.isna(a) or pd.isna(b):
        return False
    a, b = float(a), float(b)
    return abs(a - b) <= max(abs_tol, tol_pct / 100.0 * max(abs(a), abs(b)))


def _dates_compatible(a, b) -> bool:
    if a is None or b is None or pd.isna(a) or pd.isna(b):
        return True
    return pd.Timestamp(a).date() == pd.Timestamp(b).date()


def is_duplicate(a, b, p: DupParams = DupParams()) -> bool:
    """a, b: wiersze (Series/dict) z kolumnami start_/end_ country i postal, weight_t, ldm, load_date."""
    if not (_close(a["weight_t"], b["weight_t"], p.tolerance_pct, p.weight_abs_t)
            and _close(a["ldm"], b["ldm"], p.tolerance_pct, p.ldm_abs)):
        return False
    if not _dates_compatible(a.get("load_date"), b.get("load_date")):
        return False
    s_same = same_place(a["start_country"], a["start_postal"], b["start_country"], b["start_postal"])
    e_same = same_place(a["end_country"], a["end_postal"], b["end_country"], b["end_postal"])
    s_sim = s_same or similar_place(a["start_country"], a["start_postal"], b["start_country"], b["start_postal"], p.prefix_len)
    e_sim = e_same or similar_place(a["end_country"], a["end_postal"], b["end_country"], b["end_postal"], p.prefix_len)
    return (s_same and e_same) or (s_same and e_sim) or (s_sim and e_same)


def find_duplicates(df: pd.DataFrame, priority: pd.Series, p: DupParams = DupParams()) -> dict:
    """Zwraca {indeks duplikatu: numer oferty, którą zastępuje}. `priority`: im wyżej, tym lepsza oferta do zachowania
    (przy remisie wygrywa wcześniejsza w tabeli). Porównania tylko w obrębie podobnych par kodów (szybko dla setek ofert)."""
    if not p.enabled or df.empty:
        return {}
    buckets: dict[tuple, list] = {}
    for idx, r in df.iterrows():
        key = (str(r["start_country"]).upper(), str(r["end_country"]).upper(),
               prefix(r["start_country"], r["start_postal"], p.prefix_len),
               prefix(r["end_country"], r["end_postal"], p.prefix_len))
        buckets.setdefault(key, []).append(idx)
    order = {idx: n for n, idx in enumerate(df.index)}
    dup: dict = {}
    for members in buckets.values():
        if len(members) < 2:
            continue
        ranked = sorted(members, key=lambda i: (-float(priority.get(i, 0.0)), order[i]))
        kept: list = []
        for i in ranked:
            row = df.loc[i]
            hit = next((k for k in kept if is_duplicate(row, df.loc[k], p)), None)
            if hit is None:
                kept.append(i)
            else:
                dup[i] = df.loc[hit, "id"]
    return dup
