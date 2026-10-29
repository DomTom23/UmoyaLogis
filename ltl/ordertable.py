"""Filtrowanie tabeli zleceń (po zleceniodawcy i tekście) oraz scalanie poprawek z edytora z pełną tabelą."""
from __future__ import annotations

import pandas as pd


def filter_orders(df: pd.DataFrame, clients: list[str] | None = None, text: str = "",
                  only_no_price: bool = False) -> pd.DataFrame:
    """Widok tabeli: wybrani zleceniodawcy (dokładnie) i/lub fraza w zleceniodawcy, mieście, kodzie, numerze."""
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
