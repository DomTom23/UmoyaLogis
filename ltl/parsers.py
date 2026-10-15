"""Moduł 1: parsowanie surowego tekstu z giełd transportowych (Timocom, Trans.eu).

Jeden, tolerancyjny parser rozpoznaje oba układy (źródło wykrywane automatycznie):

* **wiersz** (Timocom, tabela): start i cel w tej samej linii, pozostałe pola w kolumnach
  rozdzielonych tabulacją / min. 3 spacjami / znakiem "|",
* **blok** (Trans.eu): start, data, cel, data, "waga t · nadwozie", km, cena w osobnych liniach.

Wyciągane są lokalizacje (kraj + kod pocztowy + miasto), daty, waga, LDM, dystans i cena
(liczby w formatach 1 200 | 1.200,50 | 1,200.50, waga także w kg).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import pandas as pd

FULL_TRUCK_LDM = 13.6
FULL_TRUCK_TONS = 24.0

COUNTRIES = set(
    "AL AT BA BE BG BY CH CY CZ DE DK EE ES FI FR GB GR HR HU IE IS IT LI LT LU "
    "LV MA MD ME MK MT NL NO PL PT RO RS RU SE SI SK TR UA XK".split()
)
# stare oznaczenia jednoliterowe, akceptowane tylko z myślnikiem: "D-80331"
_ONE_LETTER = {"D": "DE", "F": "FR", "I": "IT", "A": "AT", "B": "BE", "E": "ES", "H": "HU",
               "L": "LU", "N": "NO", "P": "PT", "S": "SE", "CH": "CH"}
_SPACED_5 = {"SE", "CZ", "SK", "GR"}   # "123 45"

COLUMNS = [
    "id", "source", "start_country", "start_postal", "start_city",
    "end_country", "end_postal", "end_city", "load_date", "unload_date",
    "weight_t", "ldm", "distance_km", "price_eur", "status", "defaults_used",
]

_LOC_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:(?P<cc>[A-Z]{2})(?![A-Za-z])|(?P<c1>[A-Z])(?=-\d))[\s\-:]{0,2}"
    r"(?P<p1>(?=[A-Z0-9\-]*\d)[A-Z0-9][A-Z0-9\-]{1,9})(?![\w:])"
)
_P2_RE = re.compile(r" (\d[A-Z]{0,2}|[A-Z]{2}|\d{2})(?![\w:])")
_CITY_STOP_RE = re.compile(r"\s+[-–—]\s+|[→>|;]|\s\d|\s*\(|\s{2,}")

_NUM = r"\d[\d\s., ']*?"
_DATE_A = re.compile(r"(?<![\d.,])(\d{1,2})\.(\d{1,2})\.(\d{4}|\d{2})?(?!\d)")
_DATE_B = re.compile(r"(?<![\d.,])(\d{1,2})\.(0[1-9]|1[0-2])(?![\d.,])")
_DATE_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DATE_SL = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4}|\d{2})\b")
_DIST_RE = re.compile(rf"({_NUM})\s*km\b", re.I)
_NUM_STRICT = r"\d{1,3}(?:[ .,']\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?"
_PRICE_RE = re.compile(rf"({_NUM})\s*(?:EUR|€)|(?:EUR|€)\s*({_NUM_STRICT})", re.I)
_KG_RE = re.compile(rf"(?<![\w.,])({_NUM})\s*kg\b", re.I)
_TON_RE = re.compile(r"(?<![\w.,])(\d+(?:[.,]\d+)?)\s*(?:t|tn|ton|tony|tons|tonnes)\b(?!\w)", re.I)
_LDM_RE = re.compile(
    r"(?<![\w.,])(\d{1,2}(?:[.,]\d{1,2})?)\s*(?:ldm|lm|m\b)|\bldm\s*[:=]?\s*(\d{1,2}(?:[.,]\d{1,2})?)", re.I)
_BARE_RE = re.compile(r"^\d{1,5}(?:[.,]\d{1,2})?$")
_NEGOTIABLE_RE = re.compile(r"negoc|verhandl|na\s+zapytanie|request|\bask\b|\bvb\b", re.I)
_CELL_SPLIT_RE = re.compile(r"\t|\s{3,}|\s\|\s|\|")


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
    row: bool = False                       # start i cel w jednej linii
    loose: list = field(default_factory=list)  # liczby bez jednostek


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
        if s.count(sep) > 1 or (len(tail) == 3 and head and len(head) <= 3 and sep == "."):
            s = s.replace(sep, "")
        else:
            s = head + "." + tail
    try:
        return float(s)
    except ValueError:
        return None


def find_locations(text: str) -> list[tuple[int, int, tuple]]:
    """Wszystkie lokalizacje w tekście: [(początek, koniec, (kraj, kod, miasto)), ...]."""
    found = []
    for m in _LOC_RE.finditer(text):
        country = m.group("cc") or _ONE_LETTER.get(m.group("c1"))
        if country not in COUNTRIES:
            continue
        postal, end = m.group("p1"), m.end()
        if postal.isdigit() and len(postal) < 3:
            continue
        if not any(ch.isdigit() for ch in postal):
            continue
        p2 = _P2_RE.match(text, end)
        if p2:
            extra = p2.group(1)
            ok = (
                (country in ("GB", "IE") and re.fullmatch(r"\d[A-Z]{0,2}|[A-Z]\d{1,2}", extra))
                or (country == "NL" and re.fullmatch(r"[A-Z]{2}", extra) and postal.isdigit())
                or (country in _SPACED_5 and postal.isdigit() and len(postal) == 3 and len(extra) == 2 and extra.isdigit())
            )
            if ok:
                postal, end = f"{postal} {extra}", p2.end()
        found.append((m.start(), end, (country, postal, "")))
    # miasto = tekst do następnej lokalizacji / separatora
    out = []
    for i, (s, e, loc) in enumerate(found):
        limit = found[i + 1][0] if i + 1 < len(found) else len(text)
        tail = text[e:limit]
        cut = _CITY_STOP_RE.search(tail)
        city = (tail[: cut.start()] if cut else tail).strip(" ,-–—·")
        if not re.search(r"[^\W\d_]", city) or len(city) > 40:
            city = ""
        out.append((s, e, (loc[0], loc[1], city)))
    return out


def parse_location(cell: str) -> Optional[tuple]:
    """'FR 77140 Nemours' -> ('FR', '77140', 'Nemours'); 'GB G1 1' -> ('GB', 'G1 1', '')."""
    locs = find_locations(cell)
    return locs[0][2] if locs else None


def _mk_date(d: int, m: int, y: Optional[str], today: date) -> Optional[date]:
    year = today.year if not y else int(y) + (2000 if len(y) == 2 else 0)
    try:
        return date(year, m, d)
    except ValueError:
        return None


def find_dates(text: str, today: Optional[date] = None) -> list[date]:
    today = today or date.today()
    hits = []
    for m in _DATE_ISO.finditer(text):
        hits.append((m.start(), _mk_date(int(m[3]), int(m[2]), m[1], today)))
    for m in _DATE_A.finditer(text):
        hits.append((m.start(), _mk_date(int(m[1]), int(m[2]), m[3], today)))
    for m in _DATE_B.finditer(text):
        hits.append((m.start(), _mk_date(int(m[1]), int(m[2]), None, today)))
    for m in _DATE_SL.finditer(text):
        hits.append((m.start(), _mk_date(int(m[1]), int(m[2]), m[3], today)))
    seen, out = set(), []
    for pos, d in sorted(hits, key=lambda h: h[0]):
        if d and (pos not in seen):
            seen.add(pos)
            out.append(d)
    return out


def parse_date(text: str, today: Optional[date] = None) -> Optional[date]:
    d = find_dates(text, today)
    return d[0] if d else None


def _group_number(m: re.Match) -> Optional[float]:
    for g in m.groups():
        if g and g.strip():
            return parse_number(g)
    return None


# ------------------------------------------------------- cell classifier

def _absorb(order: RawOrder, cell: str, *, phase: str) -> None:
    """Przypisuje pola z fragmentu tekstu do zlecenia. Pierwsze trafienie wygrywa.

    phase: 'row' (dowolna liczba dat: załadunek, rozładunek) | 'load' | 'unload'.
    """
    cell = cell.strip()
    if not cell:
        return
    if order.distance_km is None and (m := _DIST_RE.search(cell)):
        order.distance_km = _group_number(m)
    if order.price_eur is None and (m := _PRICE_RE.search(cell)):
        order.price_eur = _group_number(m)
    if order.price_eur is None and _NEGOTIABLE_RE.search(cell):
        order.negotiable = True

    rest = _DIST_RE.sub(" ", _PRICE_RE.sub(" ", cell))
    if order.weight_t is None and (m := _KG_RE.search(rest)):
        kg = parse_number(m.group(1))
        order.weight_t = kg / 1000 if kg else None
        rest = _KG_RE.sub(" ", rest)
    if order.weight_t is None and (m := _TON_RE.search(rest)):
        order.weight_t = parse_number(m.group(1))
    if order.ldm is None and (m := _LDM_RE.search(rest)):
        order.ldm = _group_number(m)
    rest_ton = _TON_RE.sub(" ", _LDM_RE.sub(" ", rest))

    dates = find_dates(cell)
    if dates:
        if phase == "load":
            order.load_date = order.load_date or dates[0]
        elif phase == "unload":
            order.unload_date = order.unload_date or dates[0]
        else:
            for d in dates:
                if order.load_date is None:
                    order.load_date = d
                elif order.unload_date is None:
                    order.unload_date = d
    elif _BARE_RE.match(rest_ton.strip()):
        order.loose.append(parse_number(rest_ton))


def _resolve_loose(o: RawOrder) -> None:
    """Liczby bez jednostek (kolumny tabeli Timocom): waga, LDM (małe), dystans, cena (duże)."""
    small = [x for x in o.loose if x is not None and x <= 40]
    big = [x for x in o.loose if x is not None and x > 40]
    if o.weight_t is None and small:
        o.weight_t = small.pop(0)
    if o.ldm is None and small:
        o.ldm = small.pop(0)
    if o.distance_km is None and big:
        o.distance_km = big.pop(0)
    if o.price_eur is None and big and not o.negotiable:
        o.price_eur = big.pop(0)


# --------------------------------------------------------------- parser

def parse_raw(text: str) -> list[RawOrder]:
    """Rozpoznaje zlecenia w układzie wierszowym i blokowym (także mieszanym)."""
    text = text.replace("\r", "").replace(" ", " ").replace(" ", " ")
    orders: list[RawOrder] = []
    cur: Optional[RawOrder] = None

    for line in text.split("\n"):
        if not line.strip():
            continue
        cells = [c.strip() for c in _CELL_SPLIT_RE.split(line) if c.strip()]
        locs, rests = [], []
        for cell in cells:
            found = find_locations(cell)
            locs += [f[2] for f in found]
            # usuń lokalizacje, resztę komórki oddaj klasyfikatorowi
            pieces, pos = [], 0
            for s, e, _ in found:
                pieces.append(cell[pos:s])
                pos = e
            pieces.append(cell[pos:])
            rests.append(" ".join(p for p in pieces if p.strip()))

        if len(locs) >= 2:                      # wiersz tabeli
            cur = RawOrder(start=locs[0], end=locs[1], row=True)
            orders.append(cur)
            phase = "row"
        elif len(locs) == 1:                    # blok: start albo cel
            if cur is None or (cur.start and cur.end):
                cur = RawOrder(start=locs[0])
                orders.append(cur)
            else:
                cur.end = locs[0]
            phase = "load" if cur.end is None else "unload"
        else:
            if cur is None:
                continue
            phase = "row" if cur.row else ("load" if cur.end is None else "unload")
        for r in rests:
            _absorb(cur, r, phase=phase)

    done = []
    for o in orders:
        if o.start and o.end and o.start[:2] != o.end[:2]:
            _resolve_loose(o)
            done.append(o)
    return done


def parse_timocom(text: str) -> list[RawOrder]:
    return [o for o in parse_raw(text) if o.row]


def parse_transeu(text: str) -> list[RawOrder]:
    return [o for o in parse_raw(text) if not o.row]


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
    """Źródło ('Timocom' / 'Trans.eu') jest wykrywane automatycznie (source='Auto')."""
    if not text or not text.strip():
        return pd.DataFrame(columns=COLUMNS)
    raw = parse_raw(text)
    if source == "Auto":
        rows = sum(o.row for o in raw)
        source = "Timocom" if rows >= len(raw) - rows else "Trans.eu"
    return to_dataframe(raw, source, **kwargs)
