"""Ceny szacunkowe dla zleceń bez ceny.

Zlecenie bez ceny dostaje cenę sztuczną = stawka (EUR/LDM) × LDM, wg reguły dla kraju załadunku lub rozładunku.
Do wariantów trafia tylko wtedy, gdy ta cena osiąga minimalny przychód reguły; inaczej (albo gdy żadna reguła
nie pasuje) zostaje wyłączone, jak „Do negocjacji”. Dotyczy tylko tras międzynarodowych. Gdy pasuje kilka reguł, obowiązuje ta o najwyższej stawce.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

FROM, TO = "from", "to"


@dataclass(frozen=True)
class PriceRule:
    label: str
    direction: str            # "from" = załadunek w kraju, "to" = rozładunek w kraju
    country: str              # kod ISO, np. "CH"
    eur_per_ldm: float
    min_revenue_eur: float
    enabled: bool = True


DEFAULT_RULES = (
    PriceRule("z Szwajcarii", FROM, "CH", 150.0, 500.0),
    PriceRule("do Szwajcarii", TO, "CH", 250.0, 500.0),
    PriceRule("do Anglii", TO, "GB", 300.0, 500.0),
    PriceRule("z Anglii", FROM, "GB", 150.0, 500.0),
)

RULE_COLUMNS = ["Aktywna", "Reguła", "Kierunek", "Kraj", "EUR/LDM", "Min. przychód (EUR)"]
DIRECTION_LABELS = {FROM: "z kraju (załadunek)", TO: "do kraju (rozładunek)"}


def rules_to_frame(rules) -> pd.DataFrame:
    return pd.DataFrame([[r.enabled, r.label, DIRECTION_LABELS[r.direction], r.country, r.eur_per_ldm,
                          r.min_revenue_eur] for r in rules], columns=RULE_COLUMNS)


def frame_to_rules(df: pd.DataFrame) -> tuple[PriceRule, ...]:
    """Odwrotność rules_to_frame; niepoprawne wiersze (brak kraju, stawki) są pomijane."""
    rev = {v: k for k, v in DIRECTION_LABELS.items()}
    out = []
    for r in df.itertuples(index=False):
        country = str(r[3] or "").strip().upper()
        direction = rev.get(r[2])
        if len(country) != 2 or direction is None or pd.isna(r[4]) or float(r[4]) <= 0:
            continue
        out.append(PriceRule(str(r[1] or f"{r[2]} {country}"), direction, country, float(r[4]),
                             0.0 if pd.isna(r[5]) else float(r[5]), bool(r[0])))
    return tuple(out)


def _matching(row, rules) -> list[PriceRule]:
    if row["start_country"] == row["end_country"]:
        return []        # krajowe (np. GB -> GB) nie podpadają pod stawki "z/do kraju"
    return [r for r in rules if r.enabled and (
        (r.direction == FROM and row["start_country"] == r.country)
        or (r.direction == TO and row["end_country"] == r.country))]


def apply_price_rules(orders: pd.DataFrame, rules, enabled: bool = True) -> pd.DataFrame:
    """Dodaje kolumny: price_used_eur (cena do optymalizacji), price_kind ('oferta' | 'szacunek' | 'brak'),
    price_note (opis reguły / powodu wykluczenia)."""
    df = orders.copy()
    used, kind, note = [], [], []
    for _, row in df.iterrows():
        if pd.notna(row["price_eur"]):
            used.append(float(row["price_eur"])), kind.append("oferta"), note.append("")
            continue
        matches = _matching(row, rules) if enabled else []
        if not matches:
            used.append(float("nan")), kind.append("brak"), note.append("brak ceny i pasującej reguły")
            continue
        rule = max(matches, key=lambda r: r.eur_per_ldm)
        est = round(float(row["ldm"]) * rule.eur_per_ldm, 2)
        if est + 1e-9 >= rule.min_revenue_eur:
            used.append(est), kind.append("szacunek")
            note.append(f"{rule.label}: {row['ldm']:g} LDM × {rule.eur_per_ldm:g} EUR")
        else:
            used.append(float("nan")), kind.append("brak")
            note.append(f"{rule.label}: szacunek {est:.0f} EUR poniżej minimum {rule.min_revenue_eur:.0f} EUR")
    df["price_used_eur"], df["price_kind"], df["price_note"] = used, kind, note
    return df
