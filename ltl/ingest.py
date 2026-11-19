"""Elastyczne wczytywanie danych: wklejony tekst i/lub pliki (txt, csv, tsv, xlsx), wiele źródeł naraz."""
from __future__ import annotations

import csv
import io

import pandas as pd

from .parsers import COLUMNS, TEXT_COLUMNS, parse_orders
from .tabular import has_header, rows_to_text

TEXT_ENCODINGS = ("utf-8-sig", "cp1250", "latin-1")


class UnreadableFile(Exception):
    """Plik nie dał się odczytać (uszkodzony, nieobsługiwany format, brak biblioteki)."""


def _decode(data: bytes) -> str:
    # Excel „Tekst Unicode” i Notatnik „Unicode” zapisują UTF-16: bez tego tekst wychodzi jako śmieci
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", errors="replace")
    head = data[:400]
    if head.count(b"\x00") > len(head) // 4:
        enc = "utf-16-le" if head[1:2] == b"\x00" else "utf-16-be"
        return data.decode(enc, errors="replace")
    for enc in TEXT_ENCODINGS:
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def read_uploaded(name: str, data: bytes) -> str:
    """Zamienia plik na tekst zrozumiały dla parsera (wiersze rozdzielone tabulacjami).

    Arkusze i pliki z nagłówkami (kraj, kod, miasto, data, waga, LDM, cena…) są składane po nagłówkach
    (patrz ltl.tabular). Zgłasza UnreadableFile z opisem problemu."""
    ext = name.lower().rsplit(".", 1)[-1] if "." in name else "txt"
    if ext in ("xlsx", "xlsm", "xls"):
        try:
            sheets = pd.read_excel(io.BytesIO(data), sheet_name=None, header=None, dtype=str)
        except ImportError as e:
            raise UnreadableFile("brak biblioteki do plików .xls (zainstaluj xlrd) - zapisz plik jako .xlsx lub .csv") from e
        except Exception as e:                      # uszkodzony plik, zły format, plik chroniony hasłem
            raise UnreadableFile(f"nie udało się otworzyć arkusza ({type(e).__name__}) - zapisz go jako .xlsx lub .csv") from e
        return "\n".join(t for t in (rows_to_text(df.fillna("").values.tolist()) for df in sheets.values()) if t)
    text = _decode(data)
    if ext in ("csv", "tsv", "txt"):
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
            rows = list(csv.reader(io.StringIO(text), dialect))
        except csv.Error:
            return text
        if ext == "txt" and not has_header(rows):       # wklejka z giełdy zapisana do .txt zostaje bez zmian
            return text
        return rows_to_text(rows)
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


def parse_sources_report(named: list[tuple[str, str]], **kwargs) -> tuple[pd.DataFrame, list[dict]]:
    """Parsuje każde źródło (nazwa, tekst) osobno - każde może mieć inny format - łączy wyniki, usuwa powtórzenia
    i nadaje unikalne numery. Raport: dla każdego źródła ile ofert rozpoznano i ile z nich weszło po usunięciu
    powtórzeń (np. ten sam plik wklejony drugi raz)."""
    frames, report = [], []
    for k, (name, text) in enumerate(named):
        if not text or not text.strip():
            report.append({"name": name, "found": 0, "kept": 0, "empty": True})
            continue
        f = parse_orders(text, "Auto", **kwargs)
        report.append({"name": name, "found": len(f), "kept": 0, "empty": False})
        if not f.empty:
            frames.append(f.assign(_src=k))
    if not frames:
        return pd.DataFrame(columns=COLUMNS), report
    df = drop_duplicate_offers(pd.concat(frames, ignore_index=True))
    for k, n in df["_src"].value_counts().items():
        report[int(k)]["kept"] = int(n)
    df = df.drop(columns="_src")
    df["id"] = [f"{s[0]}{i}" for i, s in enumerate(df["source"], start=1)]
    return df, report


def parse_sources(texts: list[str], **kwargs) -> pd.DataFrame:
    return parse_sources_report([(f"źródło {i}", t) for i, t in enumerate(texts, start=1)], **kwargs)[0]


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
