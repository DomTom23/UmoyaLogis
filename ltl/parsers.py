"""Moduł 1: parsowanie surowego tekstu z giełd transportowych (Timocom, Trans.eu).

Jeden, tolerancyjny parser rozpoznaje oba układy (źródło wykrywane automatycznie):

* **wiersz** (Timocom, tabela): start i cel w tej samej linii, pozostałe pola w kolumnach
  rozdzielonych tabulacją / min. 3 spacjami / znakiem "|",
* **blok** (Trans.eu): start, data, cel, data, "waga t · nadwozie", km, cena w osobnych liniach,
* **karta** (Trans.eu, kopia z ekranu): lokalizacja, daty, cel, daty, "waga, nadwozie, LDM", "km, pojazdy, FTL/LTL,
  dodatki", opis, cena, termin płatności, data i godzina publikacji. W tym trybie opisy ofert nie są
  mylone z lokalizacjami, a koniec oferty wyznacza znacznik publikacji.

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
    "weight_t", "ldm", "distance_km", "price_eur", "client", "load_type", "vehicles", "body", "extras",
    "description", "status", "defaults_used",
]
TEXT_COLUMNS = ("source", "start_city", "end_city", "defaults_used", "client", "load_type", "vehicles", "body",
                "extras", "description")

# kursy do EUR (wartości orientacyjne, można nadpisać parametrem `fx` / `pln_per_eur`)
DEFAULT_PLN_PER_EUR = 4.25
FX_TO_EUR = {"EUR": 1.0, "PLN": 1 / DEFAULT_PLN_PER_EUR, "GBP": 1.16, "CHF": 1.07}
MIN_REALISTIC_PRICE_EUR = 30.0     # oferty typu "1 EUR" to placeholdery, nie ceny
VEHICLE_WORDS = {"bus", "z przyczepą", "z naczepą", "solo"}
BODY_WORDS = {"mega", "jumbo", "standard", "firanka", "chłodnia", "izoterma", "skrzynia", "sztywna zabudowa",
              "walkingfloor", "coilmulde", "platforma", "niskopodwoziowy", "niskopodwoziowa", "inne", "hakówka",
              "cysterna", "kontener", "wywrotka", "plandeka"}

_LOC_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:(?P<cc>[A-Z]{2})(?![A-Za-z])|(?P<c1>[A-Z])(?=-\d))[\s\-:]{0,2}"
    r"(?P<p1>(?=[A-Z0-9\-]*\d)[A-Z0-9][A-Z0-9\-]{1,9})(?![\w:])"
)
_P2_RE = re.compile(r" (\d[A-Z]{0,2}|[A-Z]{2}|\d{2})(?![\w:])")
_CITY_STOP_RE = re.compile(r"\s+[-–—]\s+|[→>|;]|\s\d|\s*\(|\s{2,}")

_NUM = r"\d[\d\s., ']*?"
_DATE_A = re.compile(r"(?<![\d.,])(\d{1,2})\.(\d{1,2})\.(\d{4}|\d{2})?(?!\d)")
_DATE_B = re.compile(r"(?<![\d.,])(\d{1,2})\.(0[1-9]|1[0-2])(?![\d.]|,\d)")
_DATE_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DATE_SL = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4}|\d{2})\b")
_DIST_RE = re.compile(rf"({_NUM})\s*km\b", re.I)
_NUM_STRICT = r"\d{1,3}(?:[ .,']\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?"
_CUR = r"EUR|€|PLN|zł|zl|GBP|£|CHF"
_PRICE_RE = re.compile(
    rf"(?P<n1>{_NUM})\s*(?P<c1>{_CUR})(?![A-Za-z])(?P<t1>/t\b)?|(?P<c2>{_CUR})\s*(?P<n2>{_NUM_STRICT})", re.I)
_PRICE_SLOT_RE = re.compile(rf"^[-–—]\s*(?:{_CUR})?(?:/t)?$", re.I)          # "-", "- EUR", "- PLN/t"
_PRICE_LINE_RE = re.compile(rf"^(?:(?P<n>\d[\d\s.,]*?)\s*(?P<c>{_CUR})(?P<t>/t)?|[-–—]\s*(?:{_CUR})?(?:/t)?)$", re.I)
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
    vertical: bool = False                  # kraj / kod / miasto w osobnych liniach (kopia z Timocom)
    loose: list = field(default_factory=list)  # liczby bez jednostek
    texts: list = field(default_factory=list)  # (nr, tekst) - fragmenty nierozpoznane jako pola
    client: str = ""
    seq: int = 0                            # licznik przetworzonych fragmentów
    price_pos: Optional[int] = None         # numer fragmentu z ceną / kreską w miejscu ceny
    currency: str = "EUR"
    per_ton: bool = False                   # cena za tonę (nie za zlecenie)
    load_type: str = ""                     # FTL / LTL
    vehicles: str = ""
    body: str = ""
    extras: str = ""
    description: str = ""
    ldm_from_dims: bool = False             # LDM oszacowane z wymiarów podanych w opisie
    published: Optional[date] = None        # data publikacji oferty (tryb karty)
    card: bool = False


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


_GB_FULL_RE = re.compile(r"^([A-Z]{1,2}\d[A-Z\d]?)(\d[A-Z]{2})$")


def normalize_postal(country: str, postal: str) -> str:
    """GB: 'OX26AA' -> 'OX2 6AA' (kody wklejane bez spacji); pozostałe kraje bez zmian."""
    if country == "GB":
        m = _GB_FULL_RE.match(postal.replace(" ", ""))
        if m and " " not in postal:
            return f"{m.group(1)} {m.group(2)}"
    return postal


def _scan_locations(text: str) -> list[tuple[int, int, str, str]]:
    """[(początek, koniec_kodu, kraj, kod), ...] - bez miasta."""
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
        found.append((m.start(), end, country, normalize_postal(country, postal)))
    return found


def find_locations(text: str) -> list[tuple[int, int, tuple]]:
    """Wszystkie lokalizacje w tekście: [(początek, koniec, (kraj, kod, miasto)), ...]."""
    found = _scan_locations(text)
    out = []
    for i, (s, e, country, postal) in enumerate(found):
        limit = found[i + 1][0] if i + 1 < len(found) else len(text)
        tail = text[e:limit]
        cut = _CITY_STOP_RE.search(tail)
        raw_city = tail[: cut.start()] if cut else tail
        city = raw_city.strip(" ,-–—·")
        if not re.search(r"[^\W\d_]", city) or len(city) > 40:
            city, consumed = "", 0
        else:
            consumed = len(raw_city)
            if cut:   # "Leeds (Garforth)": dopisek w nawiasie też należy do lokalizacji
                par = re.match(r"\s*\([^)]*\)", tail[cut.start():])
                consumed += par.end() if par else 0
        out.append((s, e + consumed, (country, postal, city)))
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


def _price_info(m: re.Match) -> tuple[Optional[float], str, bool]:
    """(kwota, waluta ISO, czy za tonę) z dopasowania _PRICE_RE."""
    cur = (m.group("c1") or m.group("c2") or "EUR").upper()
    cur = {"€": "EUR", "ZŁ": "PLN", "ZL": "PLN", "£": "GBP"}.get(cur, cur)
    return parse_number(m.group("n1") or m.group("n2")), cur, bool(m.groupdict().get("t1"))


def _group_number(m: re.Match) -> Optional[float]:
    for g in m.groups():
        if g and g.strip():
            return parse_number(g)
    return None


# ------------------------------------------------------- cell classifier

def _absorb(order: RawOrder, cell: str, *, phase: str) -> None:
    """Przypisuje pola z fragmentu tekstu do zlecenia. Pierwsze trafienie wygrywa.

    phase: 'row' (dowolna liczba dat: załadunek, rozładunek) | 'load' | 'unload'.
    Fragment, w którym nic nie rozpoznano, a są litery, trafia do `order.texts` (kandydat na zleceniodawcę).
    """
    cell = cell.strip()
    if not cell:
        return
    order.seq += 1
    hit = False
    if m := _DIST_RE.search(cell):
        hit = True
        if order.distance_km is None:
            order.distance_km = _group_number(m)
    if m := _PRICE_RE.search(cell):
        hit = True
        order.price_pos = order.seq
        amount, cur, per_ton = _price_info(m)
        if per_ton:
            order.per_ton = True
        elif order.price_eur is None:
            order.price_eur, order.currency = amount, cur
    if _PRICE_SLOT_RE.match(cell) or _PAYMENT_RE.match(cell):
        hit = True
        if _PRICE_SLOT_RE.match(cell):
            order.price_pos = order.seq          # kreska w miejscu ceny: dalsze teksty to np. firma
    if _NEGOTIABLE_RE.search(cell):
        hit = True
        order.price_pos = order.seq
        if order.price_eur is None:
            order.negotiable = True

    rest = _DIST_RE.sub(" ", _PRICE_RE.sub(" ", cell))
    if m := _KG_RE.search(rest):
        hit = True
        if order.weight_t is None:
            kg = parse_number(m.group(1))
            order.weight_t = kg / 1000 if kg else None
        rest = _KG_RE.sub(" ", rest)
    if m := _TON_RE.search(rest):
        hit = True
        if order.weight_t is None:
            order.weight_t = parse_number(m.group(1))
    if m := _LDM_RE.search(rest):
        hit = True
        if order.ldm is None:
            order.ldm = _group_number(m)
    rest_ton = _TON_RE.sub(" ", _LDM_RE.sub(" ", rest))

    dates = find_dates(cell)
    if dates:
        hit = True
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
        hit = True
        order.loose.append(parse_number(rest_ton))
    if not hit and re.search(r"[^\W\d_]", cell):
        order.texts.append((order.seq, cell))


_PAYMENT_RE = re.compile(r"^\d+\s*(?:dni|dzień|dzien|days?)$", re.I)
_NOISE_RE = re.compile(r"^(strona|page|seite|wyniki|sortuj|filtr|firma|oferty)\b|\b\d+\s*(z|of|/)\s*\d+\b", re.I)


def _pick_client(texts: list, price_pos: Optional[int]) -> str:
    """Zleceniodawca = ostatni nierozpoznany tekst PO polu ceny (w Timocom kolumna „Firma” jest ostatnia, a opis
    oferty - przed ceną). Bez pola ceny nie zgadujemy."""
    if price_pos is None:
        return ""
    for pos, t in reversed(texts):
        t = t.strip()
        if pos > price_pos and len(t) >= 2 and re.search(r"[^\W\d_]{2}", t) and not _NOISE_RE.search(t):
            return t
    return ""


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

def _join_vertical(text: str) -> tuple[str, bool]:
    """Skleja lokalizacje rozbite na linie: 'FR' / '77140' / 'Nemours' -> 'FR 77140 Nemours'."""
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    out, i, joined = [], 0, False
    while i < len(lines):
        ln = lines[i]
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        first = nxt.split()[0] if nxt.split() else ""
        if ln in COUNTRIES and any(c.isdigit() for c in first) and len(first) >= 2 and not find_dates(nxt) \
                and not re.search(r"\b(?:km|kg|eur|ldm)\b|€", nxt, re.I):
            merged, i, joined = f"{ln} {nxt}", i + 2, True
            city = lines[i] if i < len(lines) else ""
            if city and city not in COUNTRIES and not re.search(r"\d", city.split("(")[0]) \
                    and re.search(r"[^\W\d_]", city) and len(city) <= 50:
                merged += f" {city}"
                i += 1
            out.append(merged)
        else:
            out.append(ln)
            i += 1
    return "\n".join(out), joined


_COUNTRY_NAMES = {"francja", "włochy", "wielka brytania", "niemcy", "polska", "hiszpania", "belgia", "holandia",
                  "szwajcaria", "austria", "czechy", "słowacja", "węgry", "portugalia", "irlandia", "dania"}
_CARD_STAMP_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")
_TIME_ONLY_RE = re.compile(r"^\d{1,2}:\d{2}$")
_DATE_LINE_RE = re.compile(r"^\d{1,2}\.\d{1,2}\.?(?:\d{2,4})?\b")
_WEIGHT_START_RE = re.compile(r"^(\d[\d.,]*)\s*(t|kg|tony|tn)\b", re.I)
_LDM_TOKEN_RE = re.compile(r"^(\d+(?:[.,]\d+)?)\s*ldm$", re.I)
_LDM_IN_TEXT_RE = re.compile(r"(?<![\w.,])(\d{1,2}(?:[.,]\d{1,2})?)\s*ldm\b", re.I)
_KM_TOKEN_RE = re.compile(r"^(\d[\d\s.,]*)\s*km$", re.I)
_EMAIL_RE = re.compile(r"@([\w-]+(?:\.[\w-]+)*\.[a-z]{2,})", re.I)


def _is_card_text(text: str) -> bool:
    """Karta Trans.eu kończy się linią z datą publikacji, a po niej godziną."""
    return bool(re.search(r"^\s*\d{2}\.\d{2}\.\d{4}\s*\n\s*\d{1,2}:\d{2}\s*$", text, re.M))


def _card_location(line: str) -> Optional[tuple]:
    """Linia zaczynająca się od 'KRAJ KOD': całe pozostałe miasto jest nazwą (bez 'Francja' i powtórzonego kodu)."""
    heads = _scan_locations(line)
    if not heads or heads[0][0] != 0:
        return None
    _, end, country, postal = heads[0]
    parts = [p.strip() for p in line[end:].split(",")]
    parts = [p for p in parts if p and p != postal and p.lower() not in _COUNTRY_NAMES]
    return country, postal, ", ".join(parts)


def _fix_year(d: Optional[date], ref: date) -> Optional[date]:
    """Daty w ofertach nie mają roku: bierzemy rok publikacji, z korektą na przełom roku."""
    if d is None:
        return None
    d = d.replace(year=ref.year)
    delta = (d - ref).days
    if delta < -120:
        return d.replace(year=d.year + 1)
    if delta > 240:
        return d.replace(year=d.year - 1)
    return d


def _parse_card_params(o: RawOrder, line: str) -> bool:
    toks = [t.strip() for t in re.split(r",\s+", line) if t.strip()]
    low = [t.lower() for t in toks]
    if not any(t in ("FTL", "LTL") or t.lower() in VEHICLE_WORDS or _KM_TOKEN_RE.match(t) for t in toks):
        return False
    vehicles, extras = [], []
    for tok, lw in zip(toks, low):
        if (m := _KM_TOKEN_RE.match(tok)):
            o.distance_km = parse_number(m.group(1))
        elif lw in VEHICLE_WORDS:
            vehicles.append(lw)
        elif tok in ("FTL", "LTL"):
            o.load_type = tok
        else:
            extras.append(tok)
    o.vehicles, o.extras = ", ".join(vehicles), ", ".join(extras)
    return True


def _parse_card_weight_line(o: RawOrder, line: str) -> bool:
    toks = [t.strip() for t in re.split(r",\s+", line) if t.strip()]
    if not toks:
        return False
    body = []
    first_word = re.sub(r"\s*\(.*", "", toks[0]).lower()
    m = _WEIGHT_START_RE.match(toks[0])
    if not m and first_word not in BODY_WORDS:
        return False
    if m:
        value = parse_number(m.group(1))
        o.weight_t = value / 1000 if (m.group(2).lower() == "kg" and value is not None) else value
        toks = toks[1:]
    for tok in toks:
        if (lm := _LDM_TOKEN_RE.match(tok)):
            o.ldm = parse_number(lm.group(1))
        else:
            body.append(tok.lower())
    o.body = ", ".join(body)
    return True


_DIMS_RE = re.compile(
    r"(?P<n>\d{1,3})\s*(?:x|×|pal\w*|pall?et\w*|colli|szt\.?)\s*:?\s*"
    r"(?P<a>\d+(?:[.,]\d+)?)\s*[x*×X]\s*(?P<b>\d+(?:[.,]\d+)?)(?:\s*[x*×X]\s*\d+(?:[.,]\d+)?)?", re.I)


def ldm_from_dimensions(text: str) -> Optional[float]:
    """LDM z opisu typu '4 pal : 120*80*143', '7 pal 80*120 + 4 pal 100*120', '2 x 150x150x80': suma
    powierzchni podłogi (n × długość × szerokość) / 2,4 m szerokości naczepy. Wymiary > 10 to cm, inaczej m."""
    total = 0.0
    for m in _DIMS_RE.finditer(text):
        n, a, b = int(m["n"]), parse_number(m["a"]), parse_number(m["b"])
        if not n or a is None or b is None:
            continue
        a, b = (a / 100, b / 100) if max(a, b) > 10 else (a, b)
        if not (0.2 <= a <= 13.6 and 0.2 <= b <= 3.0 or 0.2 <= b <= 13.6 and 0.2 <= a <= 3.0):
            continue                                         # wymiary niepodobne do ładunku (np. "3 x 400 x 120 kg")
        total += n * a * b / 2.4
    return round(total, 2) if total > 0 else None


def _finish_card(o: RawOrder) -> None:
    o.card = True
    if o.ldm is None and (m := _LDM_IN_TEXT_RE.search(o.description)):   # LDM podane tylko w opisie
        o.ldm = parse_number(m.group(1))
    if o.ldm is None and (dims := ldm_from_dimensions(o.description)):   # albo wyliczone z wymiarów palet / paczek
        o.ldm, o.ldm_from_dims = min(dims, FULL_TRUCK_LDM), True
    if not o.client and (m := _EMAIL_RE.search(o.description)):          # np. kontakt: x@tirolia.at -> tirolia.at
        o.client = m.group(1).lower()
    desc, extras = o.description.lower(), [x for x in (o.extras.split(", ") if o.extras else []) if x]
    if re.search(r"\badr\b", desc) and "ADR" not in extras:                  # ADR / winda bywają tylko w opisie
        extras.append("ADR")
    if re.search(r"tail\s*-?\s*lift|winda|\blift\b|innolift", desc) and not any(x.lower() == "winda" for x in extras):
        extras.append("Winda")
    o.extras = ", ".join(extras)
    if o.published:
        o.load_date, o.unload_date = _fix_year(o.load_date, o.published), _fix_year(o.unload_date, o.published)


def _parse_cards(text: str) -> list[RawOrder]:
    orders: list[RawOrder] = []
    cur: Optional[RawOrder] = None
    stage = "idle"                        # idle | start | end
    price_seen = weight_seen = params_seen = False

    def close() -> None:
        nonlocal cur, stage
        if cur is not None:
            _finish_card(cur)
        cur, stage = None, "idle"

    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        loc = _card_location(line)
        # nowa oferta: po zamknięciu poprzedniej, a bez znacznika publikacji - po polu ceny
        if loc and (stage == "idle" or (stage == "end" and price_seen)):
            close()
            cur = RawOrder(start=loc, card=True)
            orders.append(cur)
            stage, price_seen, weight_seen, params_seen = "start", False, False, False
            continue
        if stage == "idle":
            continue
        if stage == "start":
            if loc:
                cur.end, stage = loc, "end"
            elif cur.load_date is None and (d := find_dates(line)):
                cur.load_date = d[0]
            continue
        # stage == "end": linie po celu
        if _CARD_STAMP_RE.match(line) and (weight_seen or price_seen or params_seen):
            d = find_dates(line)
            cur.published = d[0] if d else None
            continue
        if _TIME_ONLY_RE.match(line):
            close()
            continue
        if not (weight_seen or params_seen) and _DATE_LINE_RE.match(line) and (d := find_dates(line)):
            if cur.unload_date is None:
                cur.unload_date = d[0]
            continue
        if not price_seen and (pm := _PRICE_LINE_RE.match(line)) and (params_seen or weight_seen):
            price_seen = True
            if pm.group("n"):
                m = _PRICE_RE.search(line)
                if m:
                    amount, cur_code, per_ton = _price_info(m)
                    cur.per_ton = per_ton or bool(pm.group("t"))
                    if not cur.per_ton:
                        cur.price_eur, cur.currency = amount, cur_code
            elif "/t" in line.lower():
                cur.per_ton = True
            continue
        if price_seen:
            continue                      # termin płatności, "-" itp.
        if not weight_seen and _parse_card_weight_line(cur, line):
            weight_seen = True
            continue
        if not params_seen and _parse_card_params(cur, line):
            params_seen = True
            continue
        cur.description = f"{cur.description} | {line}" if cur.description else line
    close()
    return orders


def parse_raw(text: str) -> list[RawOrder]:
    """Rozpoznaje zlecenia w układzie wierszowym, blokowym i kartowym (także mieszanym)."""
    text = text.replace("\r", "").replace("\u00a0", " ").replace("\u202f", " ")
    if _is_card_text(text):
        orders = _parse_cards(text)
        return [o for o in orders if o.start and o.end and o.start[:2] != o.end[:2]]
    text, vertical = _join_vertical(text)
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
            o.client = o.client or _pick_client(o.texts, o.price_pos)
            o.vertical = vertical and not o.row
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
    pln_per_eur: float = DEFAULT_PLN_PER_EUR,
) -> pd.DataFrame:
    """Normalizuje zlecenia do DataFrame, uzupełnia braki i oznacza podejrzane dane.

    * brak LDM  -> proporcjonalnie do wagi (waga/max_waga*max_ldm), a gdy brak obu: default_ldm
    * brak wagi -> proporcjonalnie do LDM, a gdy brak obu: default_weight_t
    * brak ceny -> status "Do negocjacji" (nie bierze udziału w optymalizacji)
    * cena w PLN/GBP/CHF jest przeliczana na EUR; ceny nierealne (< 30 EUR) i ceny za tonę są odrzucane
    * waga > 24 t lub LDM > 13,6 -> status "Za duże dla naczepy" (wyłączone z wariantów)
    """
    fx = {**FX_TO_EUR, "PLN": 1.0 / pln_per_eur}
    rows = []
    for i, o in enumerate(raw, start=1):
        used = []
        weight, ldm = o.weight_t, o.ldm
        if o.ldm_from_dims:
            used.append("LDM z wymiarów w opisie")
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

        price = None
        if o.per_ton:
            used.append("cena za tonę - pominięto")
        elif o.price_eur and o.price_eur > 0:
            price = o.price_eur * fx.get(o.currency, 1.0)
            if o.currency != "EUR":
                used.append(f"cena z {o.currency}: {o.price_eur:,.0f} → {price:,.0f} EUR".replace(",", " "))
            if price < MIN_REALISTIC_PRICE_EUR:
                used.append(f"cena nierealna ({price:,.0f} EUR) - pominięto")
                price = None
            else:
                price = round(price, 2)
        if price is None and drop_without_price:
            continue
        too_big = weight > max_weight_t + 1e-9 or ldm > max_ldm + 1e-9
        if weight > max_weight_t + 1e-9:
            used.append(f"waga {weight:g} t > {max_weight_t:g} t")
        if ldm > max_ldm + 1e-9:
            used.append(f"LDM {ldm:g} > {max_ldm:g}")
        rows.append({
            "id": f"{source[0]}{i}",
            "source": source,
            "start_country": o.start[0], "start_postal": o.start[1], "start_city": o.start[2],
            "end_country": o.end[0], "end_postal": o.end[1], "end_city": o.end[2],
            "load_date": o.load_date, "unload_date": o.unload_date,
            "weight_t": round(weight, 2), "ldm": round(ldm, 2),
            "distance_km": o.distance_km,
            "price_eur": price,
            "client": o.client,
            "load_type": o.load_type, "vehicles": o.vehicles, "body": o.body, "extras": o.extras,
            "description": o.description,
            "status": "Za duże dla naczepy" if too_big else ("OK" if price is not None else "Do negocjacji"),
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
        vertical = sum(o.vertical for o in raw)
        source = "Timocom" if rows + vertical >= len(raw) - rows - vertical else "Trans.eu"
    return to_dataframe(raw, source, **kwargs)
