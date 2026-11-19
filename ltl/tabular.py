"""Pliki tabelaryczne (xlsx, csv, tsv) -> wiersze tekstowe zrozumiałe dla parsera.

Arkusze mają zwykle nagłówki i rozbite kolumny (kraj, kod, miasto osobno; data jako data Excela). Rozpoznajemy
kolumny po nagłówkach i składamy z nich wiersz w formacie z giełdy („DE 20095 Hamburg ⇥ PL 00-001 Warszawa ⇥ …”).
Gdy nagłówków nie ma, łączymy sąsiednie komórki „kraj” + „kod”.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime

COUNTRY_NAMES = {
    "polska": "PL", "poland": "PL", "polen": "PL", "niemcy": "DE", "germany": "DE", "deutschland": "DE",
    "francja": "FR", "france": "FR", "wlochy": "IT", "włochy": "IT", "italy": "IT", "italia": "IT",
    "hiszpania": "ES", "spain": "ES", "belgia": "BE", "belgium": "BE", "holandia": "NL", "niderlandy": "NL",
    "netherlands": "NL", "luksemburg": "LU", "luxembourg": "LU", "austria": "AT", "szwajcaria": "CH",
    "switzerland": "CH", "wielka brytania": "GB", "united kingdom": "GB", "uk": "GB", "anglia": "GB",
    "czechy": "CZ", "czech republic": "CZ", "slowacja": "SK", "słowacja": "SK", "wegry": "HU", "węgry": "HU",
    "dania": "DK", "denmark": "DK", "szwecja": "SE", "portugalia": "PT", "irlandia": "IE", "litwa": "LT",
    "lotwa": "LV", "łotwa": "LV", "estonia": "EE", "rumunia": "RO", "bulgaria": "BG", "slowenia": "SI",
    "słowenia": "SI", "chorwacja": "HR", "grecja": "GR", "norwegia": "NO", "finlandia": "FI",
}


def _norm(s) -> str:
    s = unicodedata.normalize("NFKD", str(s or "").lower().replace("ł", "l"))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", s).strip()


_CLIENT = re.compile(r"klient|zleceniodawc|firma|company|customer|kunde|nadawca|shipper|zaladowca|kontrahent")
_DESC = re.compile(r"opis|description|uwagi|notes?\b|remarks|komentarz|bemerkung")
_END = re.compile(r"odbiorc|roz|dostaw|dowoz|unload|deliver|dest|\bcel\b|dokad|\bto\b|\bdo\b|\bkon|entlad|bis\b")
_START = re.compile(r"zal|odbior|load|pick|origin|skad|\bfrom\b|\bz\b|start|poczat|beladen|von\b|abhol")
_COUNTRY = re.compile(r"kraj|country|land\b|pays|panstwo")
_POSTAL = re.compile(r"kod|postal|zip|plz|\bcap\b|post code|postcode")
_CITY = re.compile(r"miasto|city|miejscowosc|\bort\b|town|stadt|\bmiejsc")
_PLACE = re.compile(r"lokaliz|location|adres|address|place|miejsce|trasa|route")
_DATE = re.compile(r"\bdata\b|\bdate\b|termin|datum|\bdzien\b")
_WEIGHT = re.compile(r"waga|weight|ciezar|tonaz|masa|gewicht|\bt\b|\bkg\b|ton\b")
_LDM = re.compile(r"ldm|lademeter|loading met|metry|\bm\b")
_PRICE = re.compile(r"cena|price|stawka|preis|fracht|kwota|wartosc|\brate\b|\beur\b|\bpln\b|przychod|wynagrodz")
_DIST = re.compile(r"\bkm\b|dystans|distance|odleglosc|entfernung")
_TYPE = re.compile(r"^typ\b|type\b|ftl|ltl")


def _classify(header: str):
    """-> (pole, strona) lub None; pole: client|desc|country|postal|city|place|date|weight|ldm|price|dist|type."""
    h = _norm(header)
    if not h:
        return None
    if _CLIENT.search(h) and not _COUNTRY.search(h):
        return ("client", None)
    if _DESC.search(h):
        return ("desc", None)
    side = "end" if _END.search(h) else "start" if _START.search(h) else None
    for field, rx in (("dist", _DIST), ("ldm", _LDM), ("weight", _WEIGHT), ("price", _PRICE), ("date", _DATE),
                      ("postal", _POSTAL), ("country", _COUNTRY), ("city", _CITY), ("place", _PLACE),
                      ("type", _TYPE)):
        if rx.search(h):
            return (field, side)
    if side:                                   # sama „Załadunek” / „Rozładunek” = pełna lokalizacja w jednej kolumnie
        return ("place", side)
    return None


def _currency(header: str) -> str:
    h = _norm(header)
    return "PLN" if re.search(r"pln|zl\b", h) else "GBP" if "gbp" in h else "CHF" if "chf" in h else "EUR"


def _date_text(v: str) -> str:
    v = str(v).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(v[:19] if "T" in v or " " in v else v, fmt).strftime("%d.%m.%Y")
        except ValueError:
            continue
    return v


def _num(v: str) -> str:
    v = str(v).strip()
    return v[:-2] if re.fullmatch(r"\d+\.0", v) else v


def _country_code(v: str) -> str:
    v = str(v).strip()
    if re.fullmatch(r"[A-Za-z]{2}", v):
        return v.upper()
    return COUNTRY_NAMES.get(_norm(v), COUNTRY_NAMES.get(v.lower(), v.upper()[:2]))


def _find_header(rows: list[list[str]]):
    """Pierwszy z kilku początkowych wierszy, w którym rozpoznano nagłówki lokalizacji załadunku i rozładunku."""
    for i, row in enumerate(rows[:6]):
        cols = {}
        for j, cell in enumerate(row):
            c = _classify(cell)
            if c and (c[0], c[1]) not in cols:
                cols[(c[0], c[1])] = j
        starts = {f for (f, s) in cols if s == "start" and f in ("place", "country", "postal")}
        ends = {f for (f, s) in cols if s == "end" and f in ("place", "country", "postal")}
        if starts and ends and (("place" in starts or "postal" in starts) and ("place" in ends or "postal" in ends)):
            return i, cols
    return None


def _place(row, cols, side, header_row) -> str:
    get = lambda f: str(row[cols[(f, side)]]).strip() if (f, side) in cols and cols[(f, side)] < len(row) else ""
    place = get("place")
    if place:
        return place
    postal, city, country = _num(get("postal")), get("city"), get("country")
    if not postal:
        return ""
    code = _country_code(country) if country else ""
    if code and re.match(rf"^{code}[\s-]", postal.upper()):
        code = ""                                  # kod już ma przedrostek kraju
    return " ".join(x for x in (code, postal, city) if x)


def has_header(rows: list[list[str]]) -> bool:
    return _find_header([[str(c or "").strip() for c in r] for r in rows if any(r)]) is not None


def rows_to_text(rows: list[list[str]]) -> str:
    """Wiersze arkusza -> tekst. Z nagłówkami: składanie pól; bez: łączenie komórek kraj + kod."""
    rows = [[("" if c is None else str(c)).strip() for c in r] for r in rows]
    rows = [r for r in rows if any(r)]
    found = _find_header(rows)
    if not found:
        return "\n".join("\t".join(_merge_country_cells(r)) for r in rows)
    hi, cols = found
    header = rows[hi]
    out = []
    for row in rows[hi + 1:]:
        a, b = _place(row, cols, "start", header), _place(row, cols, "end", header)
        if not a or not b:
            continue
        cell = lambda f, s=None: (str(row[cols[(f, s)]]).strip() if (f, s) in cols and cols[(f, s)] < len(row) else "")
        date_s = cell("date", "start") or cell("date", None)
        date_e = cell("date", "end")
        parts = [a, b]
        for d in (date_s, date_e):
            if d:
                parts.append(_date_text(d))
        w = cell("weight", None) or cell("weight", "start")
        if w:
            hdr = _norm(header[cols[("weight", None)] if ("weight", None) in cols else cols[("weight", "start")]])
            parts.append(f"{_num(w)} {'kg' if 'kg' in hdr and not re.search(r'[a-z]', _norm(w)) else 't' if not re.search(r'[a-z]', _norm(w)) else ''}".strip())
        ldm = cell("ldm", None) or cell("ldm", "start")
        if ldm:
            parts.append(f"{_num(ldm)} ldm" if not re.search(r"ldm", ldm, re.I) else ldm)
        dist = cell("dist", None) or cell("dist", "start")
        if dist:
            parts.append(f"{_num(dist)} km" if not re.search(r"km", dist, re.I) else dist)
        if ("price", None) in cols or ("price", "start") in cols:
            pc = cols.get(("price", None), cols.get(("price", "start")))
            p = str(row[pc]).strip() if pc < len(row) else ""
            if p and not re.search(r"[A-Za-z€£]|zł", p):
                p = f"{_num(p)} {_currency(header[pc])}"
            parts.append(p or "-")
        client = cell("client")
        if client:
            parts.append(client)
        desc = cell("desc")
        if desc:
            parts.append(desc)
        typ = cell("type", None)
        if typ:
            parts.append(typ)
        out.append("\t".join(parts))
    return "\n".join(out)


_COUNTRY_CELL = re.compile(r"^[A-Za-z]{2}$")
_POSTAL_CELL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 \-]{1,9}$")


def _merge_country_cells(row: list[str]) -> list[str]:
    out, i = [], 0
    while i < len(row):
        if i + 1 < len(row) and _COUNTRY_CELL.match(row[i]) and _POSTAL_CELL.match(row[i + 1]) \
                and re.search(r"\d", row[i + 1]):
            city = row[i + 2] if i + 2 < len(row) and re.fullmatch(r"[^\W\d_][^\d]*", row[i + 2] or "") else ""
            out.append(" ".join(x for x in (row[i].upper(), _num(row[i + 1]), city) if x))
            i += 3 if city else 2
        else:
            out.append(row[i])
            i += 1
    return out
