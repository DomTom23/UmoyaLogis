"""Baza danych (SQLite): zapisane tabele zleceń i rankingi pod nazwą nadaną przez użytkownika.

Plik bazy: zmienna środowiskowa ``LTL_DB_PATH`` albo ``data/umoyalogis.db`` w katalogu projektu.
Każda operacja otwiera własne krótkie połączenie (bezpieczne przy wielu sesjach Streamlita).
"""
from __future__ import annotations

import io
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import pandas as pd

from .parsers import COLUMNS

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "umoyalogis.db"
KINDS = ("orders", "ranking")
DATE_COLS = ("load_date", "unload_date")
NUM_COLS = ("weight_t", "ldm", "distance_km", "price_eur")
MAX_NAME = 80


class NameExists(Exception):
    """Tabela o tej nazwie już istnieje (zapis bez nadpisywania)."""


class InvalidName(ValueError):
    pass


def db_path() -> Path:
    return Path(os.environ.get("LTL_DB_PATH") or DEFAULT_DB)


def clean_name(name: str) -> str:
    name = " ".join((name or "").split())
    if not name:
        raise InvalidName("Podaj nazwę tabeli.")
    if len(name) > MAX_NAME:
        raise InvalidName(f"Nazwa jest za długa (maks. {MAX_NAME} znaków).")
    return name


class Store:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS saved_tables (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL CHECK (kind IN ('orders', 'ranking')),
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    n_rows INTEGER NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    UNIQUE (kind, name)
                );
                CREATE TABLE IF NOT EXISTS saved_orders (
                    table_id INTEGER NOT NULL REFERENCES saved_tables(id) ON DELETE CASCADE,
                    row_no INTEGER NOT NULL,
                    order_no TEXT, source TEXT,
                    start_country TEXT, start_postal TEXT, start_city TEXT,
                    end_country TEXT, end_postal TEXT, end_city TEXT,
                    load_date TEXT, unload_date TEXT,
                    weight_t REAL, ldm REAL, distance_km REAL, price_eur REAL,
                    status TEXT, defaults_used TEXT,
                    PRIMARY KEY (table_id, row_no)
                );
                CREATE TABLE IF NOT EXISTS saved_rankings (
                    table_id INTEGER PRIMARY KEY REFERENCES saved_tables(id) ON DELETE CASCADE,
                    payload TEXT NOT NULL
                );
            """)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------ zapis
    def _upsert_header(self, c, kind: str, name: str, n_rows: int, note: str, overwrite: bool) -> int:
        now = datetime.now().isoformat(timespec="seconds")
        row = c.execute("SELECT id, created_at FROM saved_tables WHERE kind=? AND name=?", (kind, name)).fetchone()
        if row and not overwrite:
            raise NameExists(name)
        if row:
            c.execute("DELETE FROM saved_tables WHERE id=?", (row[0],))   # kaskadowo usuwa stare wiersze
        cur = c.execute(
            "INSERT INTO saved_tables (kind, name, created_at, updated_at, n_rows, note) VALUES (?,?,?,?,?,?)",
            (kind, name, row[1] if row else now, now, n_rows, note))
        return cur.lastrowid

    def save_orders(self, name: str, df: pd.DataFrame, note: str = "", overwrite: bool = False) -> str:
        name = clean_name(name)
        df = df[COLUMNS].reset_index(drop=True)
        with self._conn() as c:
            tid = self._upsert_header(c, "orders", name, len(df), note, overwrite)
            rows = []
            for n, r in df.iterrows():
                vals = []
                for col in COLUMNS:
                    v = r[col]
                    if col in DATE_COLS:
                        v = None if pd.isna(v) else pd.Timestamp(v).date().isoformat()
                    elif col in NUM_COLS:
                        v = None if pd.isna(v) else float(v)
                    else:
                        v = None if pd.isna(v) else str(v)
                    vals.append(v)
                rows.append((tid, n, *vals))
            c.executemany(f"INSERT INTO saved_orders VALUES ({','.join('?' * (2 + len(COLUMNS)))})", rows)
        return name

    def save_ranking(self, name: str, frame: pd.DataFrame, note: str = "", overwrite: bool = False) -> str:
        name = clean_name(name)
        with self._conn() as c:
            tid = self._upsert_header(c, "ranking", name, len(frame), note, overwrite)
            c.execute("INSERT INTO saved_rankings VALUES (?, ?)", (tid, frame.to_json(orient="split", force_ascii=False)))
        return name

    # ------------------------------------------------------------ odczyt
    def list_tables(self, kind: str) -> pd.DataFrame:
        with self._conn() as c:
            rows = c.execute("SELECT name, n_rows, created_at, updated_at, note FROM saved_tables "
                             "WHERE kind=? ORDER BY updated_at DESC, name", (kind,)).fetchall()
        return pd.DataFrame(rows, columns=["name", "n_rows", "created_at", "updated_at", "note"])

    def exists(self, kind: str, name: str) -> bool:
        with self._conn() as c:
            return c.execute("SELECT 1 FROM saved_tables WHERE kind=? AND name=?",
                             (kind, clean_name(name))).fetchone() is not None

    def load_orders(self, name: str) -> pd.DataFrame:
        with self._conn() as c:
            head = c.execute("SELECT id FROM saved_tables WHERE kind='orders' AND name=?", (name,)).fetchone()
            if not head:
                raise KeyError(name)
            cols = ["order_no"] + COLUMNS[1:]
            rows = c.execute(f"SELECT {', '.join(cols)} FROM saved_orders WHERE table_id=? ORDER BY row_no",
                             (head[0],)).fetchall()
        df = pd.DataFrame(rows, columns=COLUMNS)
        for col in DATE_COLS:
            df[col] = pd.to_datetime(df[col], errors="coerce").dt.date
            df[col] = df[col].where(df[col].notna(), None)
        for col in NUM_COLS:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        for col in ("source", "start_city", "end_city", "defaults_used", "status"):
            df[col] = df[col].fillna("")
        return df

    def load_ranking(self, name: str) -> pd.DataFrame:
        with self._conn() as c:
            row = c.execute("SELECT r.payload FROM saved_rankings r JOIN saved_tables t ON t.id=r.table_id "
                            "WHERE t.kind='ranking' AND t.name=?", (name,)).fetchone()
        if not row:
            raise KeyError(name)
        return pd.read_json(io.StringIO(row[0]), orient="split")

    def delete(self, kind: str, name: str) -> bool:
        with self._conn() as c:
            return c.execute("DELETE FROM saved_tables WHERE kind=? AND name=?", (kind, name)).rowcount > 0
