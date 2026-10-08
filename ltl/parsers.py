"""Moduł 1: parsowanie surowego tekstu z giełd transportowych (Timocom, Trans.eu).

Oba parsery działają na tej samej zasadzie: tekst jest dzielony na "komórki"
(tabulacje / nowe linie), a każda komórka jest klasyfikowana wyrażeniami
regularnymi (lokalizacja, data, waga, LDM, dystans, cena).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

import pandas as pd

FULL_TRUCK_LDM = 13.6
FULL_TRUCK_TONS = 24.0

COUNTRIES = set(
    "AL AT BA BE BG BY CH CY CZ DE DK EE ES FI FR GB GR HR HU IE IS IT LI LT LU "
    "LV MA MD ME MK MT NL NO PL PT RO RS RU SE SI SK TR UA XK".split()
)
# kraje, w których kod pocztowy bywa dwuczłonowy ("GB G1 1", "NL 1234 AB")
_TWO_PART_POSTAL = {"GB", "NL", "IE", "CA"}

COLUMNS = [
    "id", "source", "start_country", "start_postal", "start_city",
    "end_country", "end_postal", "end_city", "load_date", "unload_date",
    "weight_t", "ldm", "distance_km", "price_eur", "status", "defaults_used",
]

_LOCATION_RE = re.compile(r"^\s*([A-Z]{2})[\s\-:]+(\S.*?)\s*$")
_DATE_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.?(\d{2,4})?\b")
_DATE_SLASH_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\b")
_NUM = r"\d[\d\s., ']*"
_DIST_RE = re.compile(rf"({_NUM}?)\s*km\b", re.I)
_PRICE_RE = re.compile(rf"({_NUM}?)\s*(?:EUR|€)|(?:EUR|€)\s*({_NUM})", re.I)
_NEGOTIABLE_RE = re.compile(r"negoc|verhandl|na\s+zapytanie|request|ask|vb\b", re.I)
_BARE_DECIMAL_RE = re.compile(r"^\d{1,2}[.,]\d{1,2}$")


@dataclass
class RawOrder:
    """Surowe, jeszcze nie znormalizowane pola jednego zlecenia."""
    start: Optional[tuple] = None  # (kraj, kod, miasto)
    end: Optional[tuple] = None
    load_date: Optional[date] = None
    unload_date: Optional[date] = None
    weight_t: Optional[float] = None
    ldm: Optional[float] = None
    distance_km: Optional[float] = None
    price_eur: Optional[float] = None
    negotiable: bool = False
    seen: list = field(default_factory=list)


# ---------------------------------------------------------------- helpers

def parse_number(text: str) -> Optional[float]:
    """Parsuje liczby w formatach: 1 200 | 1.200,50 | 1,200.50 | 13,60 | 1200."""
    if text is None:
        return None
    s = re.sub(r"[\s ']", "", str(text))
    s = re.sub(r"[^\d.,]", "", s)
    if not s:
        return None
    if "," in s and "." in s:
        dec = "," if s.rfind(",") > s.rfind(".") else "."
        thou = "." if dec == "," else ","
        s = s.replace(thou, "").replace(dec, ".")
    elif "," in s or "." in s:
        sep = "," if "," in s else "."
        head, _, tail = s.rpartition(sep)
        # "1.200" / "1,200" (dokładnie 3 cyfry po separatorze, wiele grup) = tysiące
        if s.count(sep) > 1 or (len(tail) == 3 and head and len(head) <= 3 and sep == "."):
            s = s.replace(sep, "")
        else:
            s = head + "." + tail
    try:
        return float(s)
    except ValueError:
        return None


def parse_location(cell: str) -> Optional[tuple]:
    """'FR 77140 Nemours' -> ('FR', '77140', 'Nemours'); 'GB G1 1' -> ('GB', 'G1 1', '')."""
    m = _LOCATION_RE.match(cell)
    if not m or m.group(1) not in COUNTRIES:
        return None
    country, rest = m.group(1), m.group(2)
    tokens = rest.split()
    if not tokens or not any(ch.isdigit() for ch in tokens[0]):
        return None
    postal = tokens[0]
    idx = 1
    if country in _TWO_PART_POSTAL and len(tokens) > 1 and re.fullmatch(r"\d[A-Z]{0,2}|[A-Z]{2}", tokens[1]):
        postal += " " + tokens[1]
        idx = 2
    city = " ".join(tokens[idx:])
    return country, postal, city


def parse_date(text: str, today: Optional[date] = None) -> Optional[date]:
    today = today or date.today()
    m = _DATE_SLASH_RE.search(text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = _DATE_RE.search(text)
    if not m:
        return None
    day, month, year = int(m.group(1)), int(m.group(2)), m.group(3)
    if year is None:
        y = today.year
    else:
        y = int(year)
        if y < 100:
            y += 2000
    try:
        return date(y, month, day)
    except ValueError:
        return None


def _find_dates(cell: str) -> list[date]:
    out = []
    for m in list(_DATE_SLASH_RE.finditer(cell)) + list(_DATE_RE.finditer(cell)):
        d = parse_date(m.group(0))
        if d:
            out.append(d)
    return out


def _first_number(regex: re.Pattern, cell: str) -> Optional[float]:
    m = regex.search(cell)
    if not m:
        return None
    for g in m.groups():
        if g and g.strip():
            return parse_number(g)
    return None


def _split_cells(text: str) -> list[str]:
    cells = []
    for line in text.replace("\r", "").split("\n"):
        for c in line.split("\t"):
            c = c.strip()
            if c:
                cells.append(c)
    return cells


# ------------------------------------------------------- cell classifier

def _absorb(order: RawOrder, cell: str) -> None:
    """Przypisuje pola z komórki (lub linii) do zlecenia. Pierwsze trafienie wygrywa."""
    # dystans i cena najpierw, żeby "1250 km" nie było brane za wagę itd.
    if order.distance_km is None:
        v = _first_number(_DIST_RE, cell)
        if v is not None:
            order.distance_km = v
    if order.price_eur is None:
        v = _first_number(_PRICE_RE, cell)
        if v is not None:
            order.price_eur = v
    if _NEGOTIABLE_RE.search(cell) and order.price_eur is None:
        order.negotiable = True

    stripped = _DIST_RE.sub(" ", _PRICE_RE.sub(" ", cell))
    if order.ldm is None:
        m = re.search(rf"({_NUM}?)\s*(?:ldm|m\s*ład\w*)\b|\b(\d{{1,2}}[.,]\d{{1,2}})\s*m\b", stripped, re.I)
        if m:
            order.ldm = parse_number(m.group(1) or m.group(2))
    if order.weight_t is None:
        # tylko jednostki 't' / 'ton' - "24 t · Plandeka", "24,0 t"
        m = re.search(r"(?<![\w.,])(\d+(?:[.,]\d+)?)\s*(?:t|ton|tony|tons)\b(?!\w)", stripped, re.I)
        if m:
            order.weight_t = parse_number(m.group(1))

    for d in _find_dates(cell):
        if order.load_date is None:
            order.load_date = d
        elif order.unload_date is None:
            order.unload_date = d


# ---------------------------------------------------------------- Timocom

def _parse_timocom_row(cells: list[str]) -> Optional[RawOrder]:
    order = RawOrder()
    loose = []
    for cell in cells:
        loc = parse_location(cell)
        if loc:
            if order.start is None:
                order.start = loc
            elif order.end is None:
                order.end = loc
            continue
        before = (order.load_date, order.weight_t, order.ldm, order.distance_km, order.price_eur)
        _absorb(order, cell)
        if before == (order.load_date, order.weight_t, order.ldm, order.distance_km, order.price_eur):
            loose.append(cell)
    # samotne liczby w kolumnach, np. "24,0" (waga) i "13,60" (LDM) - Timocom w tabeli
    # często podaje je bez jednostek; kolejność kolumn: waga, metry ładowe.
    bare = [parse_number(c) for c in loose if _BARE_DECIMAL_RE.match(c)]
    bare = [b for b in bare if b is not None]
    if order.weight_t is None and bare:
        order.weight_t = bare.pop(0)
    if order.ldm is None and bare:
        order.ldm = bare.pop(0)
    if order.start and order.end:
        return order
    return None


def _parse_timocom_stream(cells: list[str]) -> list[RawOrder]:
    """Tryb dla wklejki, w której pola leżą w osobnych liniach (bez tabulacji)."""
    orders, cur = [], None
    for cell in cells:
        loc = parse_location(cell)
        if loc:
            if cur is None or (cur.start and cur.end):
                if cur is not None:
                    orders.append(cur)
                cur = RawOrder(start=loc)
            elif cur.end is None:
                cur.end = loc
            continue
        if cur is not None:
            _absorb(cur, cell)
    if cur is not None:
        orders.append(cur)
    return [o for o in orders if o.start and o.end]


def parse_timocom(text: str) -> list[RawOrder]:
    lines = [ln for ln in text.replace("\r", "").split("\n") if ln.strip()]
    if any("\t" in ln for ln in lines):
        orders = []
        for ln in lines:
            row = _parse_timocom_row([c.strip() for c in ln.split("\t") if c.strip()])
            if row:
                orders.append(row)
        if orders:
            return orders
    return _parse_timocom_stream(_split_cells(text))


# ---------------------------------------------------------------- Trans.eu

def parse_transeu(text: str) -> list[RawOrder]:
    """Bloki: adres startu, data/czas, adres celu, data/czas, 'waga t · nadwozie', km, cena."""
    orders, cur = [], None
    for cell in _split_cells(text):
        loc = parse_location(cell)
        if loc:
            if cur is None or (cur.start and cur.end):
                if cur is not None:
                    orders.append(cur)
                cur = RawOrder(start=loc)
            elif cur.end is None:
                cur.end = loc
            continue
        if cur is None:
            continue
        if cur.end is not None and cur.unload_date is None and _find_dates(cell) and cur.load_date:
            cur.unload_date = _find_dates(cell)[0]
            continue
        if cur.start and cur.end is None and _find_dates(cell) and cur.load_date is None:
            cur.load_date = _find_dates(cell)[0]
            continue
        _absorb(cur, cell)
    if cur is not None:
        orders.append(cur)
    return [o for o in orders if o.start and o.end]


# --------------------------------------------------------------- DataFrame

def to_dataframe(
    raw: list[RawOrder],
    source: str,
    *,
    default_weight_t: float = FULL_TRUCK_TONS,
    default_ldm: float = FULL_TRUCK_LDM,
    max_ldm: float = FULL_TRUCK_LDM,
    max_weight_t: float = FULL_TRUCK_TONS,
    drop_without_price: bool = False,
) -> pd.DataFrame:
    """Normalizuje zlecenia do DataFrame i uzupełnia brakujące wartości.

    * brak LDM  -> proporcjonalnie do wagi (waga/max_waga*max_ldm), a gdy brak obu: default_ldm
    * brak wagi -> proporcjonalnie do LDM, a gdy brak obu: default_weight_t
    * brak ceny -> status "Do negocjacji" (nie bierze udziału w optymalizacji)
    """
    rows = []
    for i, o in enumerate(raw, start=1):
        used = []
        weight, ldm = o.weight_t, o.ldm
        if weight is None and ldm is None:
            weight, ldm = default_weight_t, default_ldm
            used += ["waga", "LDM"]
        elif ldm is None:
            ldm = min(max_ldm, weight / max_weight_t * max_ldm)
            used.append("LDM")
        elif weight is None:
            weight = min(max_weight_t, ldm / max_ldm * max_weight_t)
            used.append("waga")
        if o.distance_km is None:
            used.append("dystans (szac.)")
        price = o.price_eur if (o.price_eur or 0) > 0 else None
        if price is None and drop_without_price:
            continue
        rows.append({
            "id": f"{source[0]}{i}",
            "source": source,
            "start_country": o.start[0], "start_postal": o.start[1], "start_city": o.start[2],
            "end_country": o.end[0], "end_postal": o.end[1], "end_city": o.end[2],
            "load_date": o.load_date, "unload_date": o.unload_date,
            "weight_t": round(weight, 2), "ldm": round(ldm, 2),
            "distance_km": o.distance_km,
            "price_eur": price,
            "status": "OK" if price is not None else "Do negocjacji",
            "defaults_used": ", ".join(used),
        })
    return pd.DataFrame(rows, columns=COLUMNS)


def parse_orders(text: str, source: str = "Auto", **kwargs) -> pd.DataFrame:
    """source: 'Timocom' | 'Trans.eu' | 'Auto' (wybiera parser, który znajdzie więcej zleceń)."""
    if not text or not text.strip():
        return pd.DataFrame(columns=COLUMNS)
    if source == "Timocom":
        return to_dataframe(parse_timocom(text), "Timocom", **kwargs)
    if source == "Trans.eu":
        return to_dataframe(parse_transeu(text), "Trans.eu", **kwargs)
    # Timocom wkleja się jako tabela (tabulacje), Trans.eu jako bloki w liniach
    if "\t" in text:
        return to_dataframe(parse_timocom(text), "Timocom", **kwargs)
    return to_dataframe(parse_transeu(text), "Trans.eu", **kwargs)
