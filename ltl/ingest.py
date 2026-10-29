"""Elastyczne wczytywanie danych: wklejony tekst i/lub pliki (txt, csv, tsv, xlsx), wiele źródeł naraz."""
from __future__ import annotations

import csv
import io

import pandas as pd

from .parsers import COLUMNS, TEXT_COLUMNS, parse_orders

TEXT_ENCODINGS = ("utf-8-sig", "cp1250", "latin-1")


def _decode(data: bytes) -> str:
    for enc in TEXT_ENCODINGS:
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def read_uploaded(name: str, data: bytes) -> str:
    """Zamienia plik na tekst zrozumiały dla parsera (wiersze tabeli rozdzielone tabulacjami)."""
    ext = name.lower().rsplit(".", 1)[-1] if "." in name else "txt"
    if ext in ("xlsx", "xlsm", "xls"):
        sheets = pd.read_excel(io.BytesIO(data), sheet_name=None, header=None, dtype=str)
        lines = []
        for df in sheets.values():
            for row in df.fillna("").itertuples(index=False):
                lines.append("\t".join(str(c).strip() for c in row))
        return "\n".join(lines)
    text = _decode(data)
    if ext in ("csv", "tsv"):
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
            rows = csv.reader(io.StringIO(text), dialect)
            return "\n".join("\t".join(c.strip() for c in r) for r in rows)
        except csv.Error:
            pass
    return text


DUPLICATE_KEY = ["start_country", "start_postal", "end_country", "end_postal", "load_date", "unload_date",
                 "weight_t", "ldm", "price_eur", "client", "load_type", "description"]


def drop_duplicate_offers(df: pd.DataFrame) -> pd.DataFrame:
    """Giełdy powtarzają te same oferty (np. ten sam ładunek 3 razy). Zostaje pierwsza, a liczba powtórzeń
    trafia do uwag - inaczej jedno zlecenie mogłoby wejść do wariantu wielokrotnie."""
    if df.empty:
        return df
    key = pd.Series(["|".join(map(str, row)) for row in df[DUPLICATE_KEY].itertuples(index=False)], index=df.index)
    counts = key.map(key.value_counts())
    out = df[~key.duplicated()].copy()
    out["defaults_used"] = [(f"{u}, " if u else "") + f"×{n} w danych" if n > 1 else u
                            for u, n in zip(out["defaults_used"], counts[out.index])]
    return out.reset_index(drop=True)


def parse_sources(texts: list[str], **kwargs) -> pd.DataFrame:
    """Parsuje każde źródło osobno (własny format), łączy wyniki, usuwa powtórzenia i nadaje unikalne numery."""
    frames = [parse_orders(t, "Auto", **kwargs) for t in texts if t and t.strip()]
    frames = [f for f in frames if not f.empty]
    if not frames:
        return pd.DataFrame(columns=COLUMNS)
    df = drop_duplicate_offers(pd.concat(frames, ignore_index=True))
    df["id"] = [f"{s[0]}{i}" for i, s in enumerate(df["source"], start=1)]
    return df


def clean_edited(df: pd.DataFrame) -> pd.DataFrame:
    """Porządkuje tabelę po ręcznej edycji: puste wiersze, typy liczb, id, status."""
    df = df.copy()
    for c in ("start_country", "end_country", "start_postal", "end_postal"):
        df[c] = df[c].fillna("").astype(str).str.strip().str.upper() if c.endswith("country") \
            else df[c].fillna("").astype(str).str.strip()
    df = df[(df["start_country"] != "") & (df["start_postal"] != "")
            & (df["end_country"] != "") & (df["end_postal"] != "")].reset_index(drop=True)
    for c in TEXT_COLUMNS:
        df[c] = df[c].fillna("").astype(str)
    for c in ("weight_t", "ldm", "distance_km", "price_eur"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["weight_t"] = df["weight_t"].fillna(24.0)
    df["ldm"] = df["ldm"].fillna(13.6)
    df.loc[df["price_eur"] <= 0, "price_eur"] = float("nan")
    df["id"] = [str(i) if isinstance(i, str) and i and i != "nan" else f"M{n}"
                for n, i in enumerate(df["id"], start=1)]
    too_big = (df["weight_t"] > 24.0 + 1e-9) | (df["ldm"] > 13.6 + 1e-9)
    df["status"] = ["Za duże dla naczepy" if big else ("OK" if pd.notna(p) else "Do negocjacji")
                    for p, big in zip(df["price_eur"], too_big)]
    return df
