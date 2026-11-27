"""Elastyczne wczytywanie danych: wklejony tekst i/lub pliki (txt, csv, tsv, xlsx), wiele źródeł naraz."""
from __future__ import annotations

import csv
import io
import re

import pandas as pd

from .parsers import COLUMNS, TEXT_COLUMNS, WINDOW_COLUMNS, parse_orders
from .schedule import sync_windows
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


EXPORT_REQUIRED = ("start_country", "start_postal", "end_country", "end_postal")


def _find_export_header(rows: list):
    for i, r in enumerate(rows[:15]):
        cells = [str(c if c is not None else "").strip().lower() for c in r]
        if all(h in cells for h in EXPORT_REQUIRED):
            return i, cells
    return None


def _export_frame(rows: list) -> pd.DataFrame | None:
    """Tabela z wierszy arkusza, jeśli ma nagłówki w formacie eksportu aplikacji (historia ofert, tabela zleceń):
    start_country, start_postal, end_country, end_postal, load_date, weight_t, ldm, price_eur…"""
    found = _find_export_header(rows)
    if not found:
        return None
    hi, cols = found
    recs = []
    for r in rows[hi + 1:]:
        cells = [("" if c is None else str(c)).strip() for c in r]
        if any(cells):
            recs.append(dict(zip(cols, cells)))
    raw = pd.DataFrame(recs)
    out = pd.DataFrame(index=raw.index)
    for c in COLUMNS:
        out[c] = raw[c] if c in raw.columns else ""
    out["id"] = ""                                      # numer z historii to nie numer zlecenia - nadamy nowe
    out["source"] = out["source"].replace("", "Plik")
    for c in ("weight_t", "ldm", "distance_km", "price_eur"):
        out[c] = pd.to_numeric(out[c].astype(str).str.replace(",", ".", regex=False).str.replace(r"\s", "", regex=True),
                               errors="coerce")
    for c in ("load_date", "unload_date"):
        d = pd.to_datetime(out[c], errors="coerce").dt.date
        out[c] = d.where(d.notna(), None)
    for c in WINDOW_COLUMNS:
        out[c] = pd.to_datetime(out[c], errors="coerce")
    return clean_edited(out)


def structured_orders(name: str, data: bytes) -> pd.DataFrame | None:
    """Plik w formacie eksportu aplikacji (np. „Pobierz CSV” z historii ofert) -> tabela zleceń; inny plik -> None."""
    ext = name.lower().rsplit(".", 1)[-1] if "." in name else "txt"
    try:
        if ext in ("xlsx", "xlsm", "xls"):
            sheets = pd.read_excel(io.BytesIO(data), sheet_name=None, header=None, dtype=str)
            frames = [f for f in (_export_frame(df.fillna("").values.tolist()) for df in sheets.values()) if f is not None]
            return pd.concat(frames, ignore_index=True) if frames else None
        if ext not in ("csv", "tsv", "txt"):
            return None
        text = _decode(data)
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
            dialect.delimiter = ";"
        return _export_frame(list(csv.reader(io.StringIO(text), dialect)))
    except Exception:
        return None


def read_source(name: str, data: bytes):
    """Wczytuje plik: gotowa tabela (format eksportu aplikacji) albo tekst do rozpoznania przez parser."""
    frame = structured_orders(name, data)
    return frame if frame is not None else read_uploaded(name, data)


def append_orders(existing: pd.DataFrame | None, new: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Dopisuje nowe oferty do bieżącej tabeli (z jej ręcznymi poprawkami): nowe, które są powtórzeniem którejś
    z istniejących, pomijamy, pozostałym nadajemy kolejne numery (T13, T14…). -> (tabela, {"added", "duplicates"})."""
    if existing is None or existing.empty:
        return new.reset_index(drop=True), {"added": len(new), "duplicates": 0}
    both = pd.concat([existing.assign(_new=False), new.assign(_new=True)], ignore_index=True)
    key = pd.Series(["|".join(map(str, row)) for row in both[DUPLICATE_KEY].itertuples(index=False)])
    dup = key.duplicated(keep="first") & both["_new"]
    both = both[~dup].copy()
    n_added = int(both["_new"].sum())
    top: dict[str, int] = {}
    for i in both.loc[~both["_new"], "id"].astype(str):
        m = re.fullmatch(r"([A-Za-z]+)(\d+)", i)
        if m:
            top[m[1]] = max(top.get(m[1], 0), int(m[2]))
    ids = []
    for i, is_new, src in zip(both["id"], both["_new"], both["source"]):
        if not is_new:
            ids.append(i)
            continue
        pre = (str(src or "N")[:1] or "N").upper()
        top[pre] = top.get(pre, 0) + 1
        ids.append(f"{pre}{top[pre]}")
    both["id"] = ids
    return both.drop(columns="_new").reset_index(drop=True), {"added": n_added, "duplicates": int(dup.sum())}


def parse_sources_report(named: list[tuple[str, str]], **kwargs) -> tuple[pd.DataFrame, list[dict]]:
    """Parsuje każde źródło (nazwa, tekst) osobno - każde może mieć inny format - łączy wyniki, usuwa powtórzenia
    i nadaje unikalne numery. Raport: dla każdego źródła ile ofert rozpoznano i ile z nich weszło po usunięciu
    powtórzeń (np. ten sam plik wklejony drugi raz)."""
    frames, report = [], []
    for k, (name, text) in enumerate(named):
        if isinstance(text, pd.DataFrame):               # plik w formacie eksportu aplikacji: gotowe kolumny, bez zgadywania
            f = text.reindex(columns=COLUMNS).copy()
            if kwargs.get("drop_without_price"):
                f = f[f["price_eur"].notna()]
        elif not text or not text.strip():
            report.append({"name": name, "found": 0, "kept": 0, "empty": True})
            continue
        else:
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
    for c in WINDOW_COLUMNS:                         # kolumny okien mogły zniknąć (np. stare zapisane tabele)
        df[c] = pd.to_datetime(df[c], errors="coerce") if c in df.columns else pd.NaT
    return sync_windows(df)
