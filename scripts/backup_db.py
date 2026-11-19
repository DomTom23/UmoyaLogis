"""Kopia zapasowa bazy (historia ofert, zapisane tabele i rankingi) - bezpieczna także przy działającej aplikacji.

Użycie:  python scripts/backup_db.py [katalog_docelowy] [ile_kopii_zostawić]
Domyślnie: ./backups, 30 ostatnich kopii. Baza: LTL_DB_PATH albo data/umoyalogis.db.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path


def backup(db: Path, dest: Path, keep: int = 30) -> Path:
    if not db.exists():
        raise FileNotFoundError(f"Brak bazy: {db}")
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / f"umoyalogis_{datetime.now():%Y-%m-%d_%H%M%S}.db"
    src, dst = sqlite3.connect(db), sqlite3.connect(out)
    try:
        src.backup(dst)                       # spójna kopia nawet w trakcie zapisu
    finally:
        dst.close()
        src.close()
    for old in sorted(dest.glob("umoyalogis_*.db"))[:-keep]:
        old.unlink()
    return out


if __name__ == "__main__":
    db = Path(os.environ.get("LTL_DB_PATH") or Path(__file__).resolve().parent.parent / "data" / "umoyalogis.db")
    dest = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "backups"
    keep = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    print("Zapisano:", backup(db, dest, keep))
