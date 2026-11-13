"""Filtrowanie tabeli zleceń (po zleceniodawcy i tekście) oraz scalanie poprawek z edytora z pełną tabelą."""
from __future__ import annotations

import pandas as pd


def _squash(x) -> str:
    return "".join(ch for ch in str(x if x is not None and x == x else "").upper() if ch.isalnum())


def parse_codes(text: str) -> list[str]:
    """Kody do wyszukiwania z pola tekstowego: rozdzielone przecinkiem, średnikiem lub nową linią.
    Każdy kod to początek „kraj+kod pocztowy” (DE 20, FR-77) albo sam początek kodu (20095, 00-0)."""
    parts = [p for chunk in str(text or "").replace(";", ",").replace("\n", ",").split(",") for p in [_squash(chunk)] if p]
    return parts


def code_match(country, postal, codes: list[str]) -> bool:
    """Czy miejsce (kraj, kod pocztowy) zaczyna się od któregoś z kodów (z krajem lub bez). Brak kodów = pasuje."""
    if not codes:
        return True
    full, pc = _squash(country) + _squash(postal), _squash(postal)
    return any(full.startswith(c) or pc.startswith(c) for c in codes)


def filter_orders(df: pd.DataFrame, clients: list[str] | None = None, text: str = "",
                  only_no_price: bool = False, from_codes: str = "", to_codes: str = "") -> pd.DataFrame:
    """Widok tabeli: wybrani zleceniodawcy (dokładnie), fraza w zleceniodawcy/mieście/kodzie/numerze oraz kody
    pocztowe załadunku i rozładunku (np. „DE 20, FR 77”: początek kodu z krajem lub bez)."""
    mask = pd.Series(True, index=df.index)
    if clients:
        mask &= df["client"].isin(clients)
    if text.strip():
        q = text.strip().lower()
        hay = df[["id", "client", "start_city", "end_city", "start_postal", "end_postal",
                  "start_country", "end_country"]].fillna("").astype(str).agg(" ".join, axis=1).str.lower()
        mask &= hay.str.contains(q, regex=False)
    if only_no_price:
        mask &= df["price_eur"].isna()
    fc, tc = parse_codes(from_codes), parse_codes(to_codes)
    if fc:
        mask &= pd.Series([code_match(c, p, fc) for c, p in zip(df["start_country"], df["start_postal"])], index=df.index)
    if tc:
        mask &= pd.Series([code_match(c, p, tc) for c, p in zip(df["end_country"], df["end_postal"])], index=df.index)
    return df[mask]


def merge_edits(work: pd.DataFrame, view_ids: list[str], edited: pd.DataFrame) -> pd.DataFrame:
    """Nanosi na pełną tabelę `work` zmiany wykonane w widoku (wiersze o numerach `view_ids`).

    Wiersze z numerem w `edited` aktualizują `work`, wiersze widoku, których już nie ma, są usuwane,
    a wiersze bez numeru (dodane w edytorze) dostają nowy numer M<n>."""
    work = work.copy().set_index("id", drop=False)
    edited = edited.reset_index(drop=True)
    has_id = edited["id"].notna() & (edited["id"].astype(str) != "")
    kept = set(edited.loc[has_id, "id"])
    work = work.drop(index=[i for i in view_ids if i not in kept and i in work.index])
    for _, row in edited[has_id].iterrows():
        if row["id"] in work.index:
            work.loc[row["id"], edited.columns] = row.values
        else:
            work.loc[row["id"]] = row.reindex(work.columns)
    taken, n = set(work.index), 0
    for _, row in edited[~has_id].iterrows():
        while f"M{n}" in taken:
            n += 1
        new_id = f"M{n}"
        taken.add(new_id)
        row = row.reindex(work.columns)
        row["id"] = new_id
        work.loc[new_id] = row
    return work.reset_index(drop=True)
