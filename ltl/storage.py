"""Baza danych (SQLite): zapisane tabele zleceń i rankingi pod nazwą nadaną przez użytkownika.

Plik bazy: zmienna środowiskowa ``LTL_DB_PATH`` albo ``data/umoyalogis.db`` w katalogu projektu.
Każda operacja otwiera własne krótkie połączenie (bezpieczne przy wielu sesjach Streamlita).
"""
from __future__ import annotations

import hashlib
import io
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import pandas as pd

from .parsers import COLUMNS, TEXT_COLUMNS

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "umoyalogis.db"
KINDS = ("orders", "ranking")
DATE_COLS = ("load_date", "unload_date")
NUM_COLS = ("weight_t", "ldm", "distance_km", "price_eur")
MAX_NAME = 80
DB_COLS = ["order_no"] + COLUMNS[1:]        # kolumna "id" tabeli zleceń to w bazie order_no
OUTCOMES = ["", "zrealizowane", "przegrane", "nieaktualne"]


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
                    client TEXT, load_type TEXT, vehicles TEXT, body TEXT, extras TEXT, description TEXT,
                    status TEXT, defaults_used TEXT,
                    PRIMARY KEY (table_id, row_no)
                );
                CREATE TABLE IF NOT EXISTS saved_rankings (
                    table_id INTEGER PRIMARY KEY REFERENCES saved_tables(id) ON DELETE CASCADE,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS offer_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fingerprint TEXT NOT NULL UNIQUE,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    seen_count INTEGER NOT NULL DEFAULT 1,
                    source TEXT,
                    start_country TEXT, start_postal TEXT, start_city TEXT,
                    end_country TEXT, end_postal TEXT, end_city TEXT,
                    load_date TEXT, unload_date TEXT,
                    weight_t REAL, ldm REAL, distance_km REAL, price_eur REAL,
                    client TEXT NOT NULL DEFAULT '',
                    load_type TEXT, vehicles TEXT, body TEXT, extras TEXT, description TEXT,
                    realized_price_eur REAL,
                    outcome TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_history_client ON offer_history(client);
                CREATE INDEX IF NOT EXISTS idx_history_seen ON offer_history(first_seen_at);
            """)
            # migracja baz utworzonych we wcześniejszych wersjach (nowe kolumny dopisujemy na końcu)
            for table, new_cols in (("saved_orders", ("client", "load_type", "vehicles", "body", "extras", "description")),
                                    ("offer_history", ("load_type", "vehicles", "body", "extras", "description"))):
                have = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
                for col in new_cols:
                    if col not in have:
                        c.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")

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
            c.executemany(f"INSERT INTO saved_orders (table_id, row_no, {', '.join(DB_COLS)}) "
                          f"VALUES ({','.join('?' * (2 + len(DB_COLS)))})", rows)
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
            rows = c.execute(f"SELECT {', '.join(DB_COLS)} FROM saved_orders WHERE table_id=? ORDER BY row_no",
                             (head[0],)).fetchall()
        df = pd.DataFrame(rows, columns=COLUMNS)
        for col in DATE_COLS:
            df[col] = pd.to_datetime(df[col], errors="coerce").dt.date
            df[col] = df[col].where(df[col].notna(), None)
        for col in NUM_COLS:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        for col in (*TEXT_COLUMNS, "status"):
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

    # ------------------------------------------------------------ historia ofert
    @staticmethod
    def _fingerprint(r) -> str:
        def n(v):
            return "" if pd.isna(v) else f"{float(v):.2f}"
        parts = [str(r["start_country"]), str(r["start_postal"]), str(r["end_country"]), str(r["end_postal"]),
                 "" if pd.isna(r["load_date"]) else str(pd.Timestamp(r["load_date"]).date()),
                 n(r["weight_t"]), n(r["ldm"]), n(r["price_eur"]), str(r.get("client", "") or "")]
        return hashlib.sha1("|".join(p.strip().lower() for p in parts).encode()).hexdigest()

    def record_offers(self, df: pd.DataFrame, seen_at: str | None = None) -> dict:
        """Zapisuje oferty do historii. Ta sama oferta (trasa, data, ładunek, cena, zleceniodawca) wklejona
        ponownie nie tworzy duplikatu - zwiększa licznik i datę ostatniego widzenia."""
        now = seen_at or datetime.now().isoformat(timespec="seconds")
        new = repeated = 0
        with self._conn() as c:
            for _, r in df.iterrows():
                fp = self._fingerprint(r)
                if c.execute("UPDATE offer_history SET last_seen_at=?, seen_count=seen_count+1 "
                             "WHERE fingerprint=?", (now, fp)).rowcount:
                    repeated += 1
                    continue
                vals = {}
                for col in ("source", "start_country", "start_postal", "start_city", "end_country", "end_postal",
                            "end_city", "client", "load_type", "vehicles", "body", "extras", "description"):
                    vals[col] = "" if pd.isna(r.get(col)) else str(r.get(col))
                for col in DATE_COLS:
                    vals[col] = None if pd.isna(r[col]) else pd.Timestamp(r[col]).date().isoformat()
                for col in NUM_COLS:
                    vals[col] = None if pd.isna(r[col]) else float(r[col])
                keys = list(vals)
                c.execute(f"INSERT INTO offer_history (fingerprint, first_seen_at, last_seen_at, {', '.join(keys)}) "
                          f"VALUES (?, ?, ?, {', '.join('?' * len(keys))})", (fp, now, now, *vals.values()))
                new += 1
        return {"new": new, "repeated": repeated}

    def history_count(self) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM offer_history").fetchone()[0]

    def history_clients(self) -> list[str]:
        with self._conn() as c:
            return [r[0] for r in c.execute(
                "SELECT client FROM offer_history WHERE client <> '' GROUP BY client ORDER BY COUNT(*) DESC, client")]

    def query_history(self, clients: list[str] | None = None, text: str = "", from_countries: list[str] | None = None,
                      to_countries: list[str] | None = None, date_from=None, date_to=None, outcome: str = "",
                      limit: int = 5000) -> pd.DataFrame:
        where, args = [], []
        if clients:
            where.append(f"client IN ({','.join('?' * len(clients))})")
            args += clients
        if text.strip():
            like = f"%{text.strip().lower()}%"
            where.append("(LOWER(client) LIKE ? OR LOWER(start_city) LIKE ? OR LOWER(end_city) LIKE ? "
                         "OR LOWER(start_postal) LIKE ? OR LOWER(end_postal) LIKE ?)")
            args += [like] * 5
        for col, vals in (("start_country", from_countries), ("end_country", to_countries)):
            if vals:
                where.append(f"{col} IN ({','.join('?' * len(vals))})")
                args += vals
        if date_from:
            where.append("substr(first_seen_at, 1, 10) >= ?")
            args.append(pd.Timestamp(date_from).date().isoformat())
        if date_to:
            where.append("substr(first_seen_at, 1, 10) <= ?")
            args.append(pd.Timestamp(date_to).date().isoformat())
        if outcome:
            where.append("outcome = ?")
            args.append(outcome)
        sql = "SELECT id, first_seen_at, last_seen_at, seen_count, source, client, start_country, start_postal, " \
              "start_city, end_country, end_postal, end_city, load_date, unload_date, weight_t, ldm, distance_km, " \
              "price_eur, load_type, vehicles, body, extras, description, realized_price_eur, outcome, note " \
              "FROM offer_history"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY first_seen_at DESC, id DESC LIMIT ?"
        with self._conn() as c:
            cur = c.execute(sql, (*args, limit))
            df = pd.DataFrame(cur.fetchall(), columns=[d[0] for d in cur.description])
        for col in DATE_COLS:
            df[col] = pd.to_datetime(df[col], errors="coerce").dt.date
            df[col] = df[col].where(df[col].notna(), None)
        return df

    def update_history(self, changes: pd.DataFrame) -> int:
        """Zapisuje cenę zrealizowaną, wynik i notatkę (kolumny: id, realized_price_eur, outcome, note)."""
        n = 0
        with self._conn() as c:
            for r in changes.itertuples(index=False):
                price = None if pd.isna(r.realized_price_eur) else float(r.realized_price_eur)
                n += c.execute("UPDATE offer_history SET realized_price_eur=?, outcome=?, note=? WHERE id=?",
                               (price, r.outcome or "", r.note or "", int(r.id))).rowcount
        return n

    def delete_history(self, ids: list[int]) -> int:
        with self._conn() as c:
            return sum(c.execute("DELETE FROM offer_history WHERE id=?", (int(i),)).rowcount for i in ids)
