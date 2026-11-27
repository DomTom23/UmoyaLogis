"""Estymacja współrzędnych kodów pocztowych i odległości drogowych (MVP).

* Współrzędne: opcjonalnie online (api.zippopotam.us), z cache na dysku;
  gdy brak sieci / kodu - środek ciężkości kraju (źródło oznaczone "kraj").
* Odległość: linia prosta (haversine) * ROAD_FACTOR (1.3).
"""
from __future__ import annotations

import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

ROAD_FACTOR = 1.3
EARTH_RADIUS_KM = 6371.0088

COUNTRY_CENTROIDS = {
    "AL": (41.15, 20.17), "AT": (47.52, 14.55), "BA": (43.92, 17.68), "BE": (50.50, 4.47),
    "BG": (42.73, 25.49), "BY": (53.71, 27.95), "CH": (46.82, 8.23), "CY": (35.13, 33.43),
    "CZ": (49.82, 15.47), "DE": (51.17, 10.45), "DK": (56.26, 9.50), "EE": (58.60, 25.01),
    "ES": (40.46, -3.75), "FI": (61.92, 25.75), "FR": (46.23, 2.21), "GB": (54.00, -2.00),
    "GR": (39.07, 21.82), "HR": (45.10, 15.20), "HU": (47.16, 19.50), "IE": (53.41, -8.24),
    "IS": (64.96, -19.02), "IT": (42.50, 12.57), "LI": (47.17, 9.56), "LT": (55.17, 23.88),
    "LU": (49.82, 6.13), "LV": (56.88, 24.60), "MA": (31.79, -7.09), "MD": (47.41, 28.37),
    "ME": (42.71, 19.37), "MK": (41.61, 21.75), "MT": (35.94, 14.38), "NL": (52.13, 5.29),
    "NO": (60.47, 8.47), "PL": (51.92, 19.15), "PT": (39.40, -8.22), "RO": (45.94, 24.97),
    "RS": (44.02, 21.01), "RU": (55.75, 37.62), "SE": (60.13, 18.64), "SI": (46.15, 14.99),
    "SK": (48.67, 19.70), "TR": (38.96, 35.24), "UA": (48.38, 31.17), "XK": (42.60, 20.90),
}

DEFAULT_CACHE = Path(os.environ.get("LTL_GEOCACHE_PATH")
                     or Path.home() / ".cache" / "umoyalogis" / "geocache.json")


def haversine_km(lat1, lon1, lat2, lon2):
    """Odległość w linii prostej; działa na skalarach i tablicach numpy."""
    lat1, lon1, lat2, lon2 = map(np.radians, (lat1, lon1, lat2, lon2))
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_KM * np.arcsin(np.sqrt(a))


def road_distance_matrix(lats, lons, factor: float = ROAD_FACTOR) -> np.ndarray:
    lats, lons = np.asarray(lats, float), np.asarray(lons, float)
    return haversine_km(lats[:, None], lons[:, None], lats[None, :], lons[None, :]) * factor


def _alnum(x) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(x or "").upper())


def postal_candidates(country: str, postal: str) -> list[str]:
    """Warianty zapisu kodu pocztowego do zapytań o położenie, od najbardziej prawdopodobnego.

    Usługi znają kody w różnych formatach (NL: 4 cyfry bez liter, CZ/SK/SE: ze spacją, PL: z myślnikiem, GB i IE: tylko
    pierwszy człon…), a użytkownik może wpisać dowolny. Zwracamy kilka wariantów; pierwszy służy też jako klucz cache."""
    c = str(country or "").strip().upper()
    raw = re.sub(r"\s+", " ", str(postal or "").strip().upper())
    if re.match(rf"^{re.escape(c)}[\s\-]+", raw) and c:                  # „FR-75001”, „DE 20095” wpisane w polu kodu
        raw = re.sub(rf"^{re.escape(c)}[\s\-]+", "", raw)
    if c == "LU" and raw.startswith("L-"):
        raw = raw[2:]
    compact = raw.replace(" ", "")
    digits = re.sub(r"\D", "", raw)
    out: list[str] = []

    def add(x: str) -> None:
        if x and x not in out:
            out.append(x)

    if c == "GB":
        m = re.match(r"^([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})?", raw)
        add(m.group(1) if m else raw.split(" ")[0])                        # outward code (G1, NN11, SW1A)
        return out
    if c == "IE":
        add(compact[:3])
        return out
    if c == "NL":
        add(compact)                                                       # 4611AA
        add(digits[:4])                                                    # 4611
        return out
    if c == "PL":
        if len(digits) == 5:
            add(f"{digits[:2]}-{digits[2:]}")
        add(compact)
        return out
    if c == "PT":
        add(compact)                                                       # 4000-001
        add(digits[:4])                                                    # 4000
        if len(digits) == 7:
            add(f"{digits[:4]}-{digits[4:]}")
        return out
    if c in ("CZ", "SK", "SE") and len(digits) == 5:
        add(f"{digits[:3]} {digits[3:]}")                                 # 110 00
        add(digits)
        return out
    if c in ("LV", "LT") and digits:
        add(compact)
        add(digits)
        return out
    add(compact)
    if digits and digits != compact:
        add(digits)
    return out


class Geocoder:
    """Kod pocztowy -> (lat, lon, źródło). Źródło: 'online' | 'cache' | 'prefiks' (przybliżone z podobnego kodu) | 'kraj'.

    Kolejność prób: cache na dysku -> zippopotam.us (kilka wariantów zapisu kodu) -> Nominatim (kod, potem kod + miasto)
    -> przybliżenie z najbliżej podobnego kodu w cache -> środek kraju. Chwilowe błędy sieci nie wyłączają geokodowania
    na stałe: usługa jest tylko wstrzymywana na minutę, a nieznane kody pamiętane krótko, żeby nie pytać w kółko."""

    PAUSE_S = 60.0           # na tyle wstrzymujemy usługę po serii błędów (albo po odmowie 429/5xx)
    MISS_TTL_S = 3600.0      # tyle pamiętamy, że usługa NIE zna danego kodu

    def __init__(self, online: bool = True, cache_path: Optional[Path] = DEFAULT_CACHE, timeout: float = 6.0):
        self.online = online                       # wybór użytkownika; błędy sieci go nie zmieniają
        self.nominatim_budget = 10 ** 9            # ile zapytań do Nominatim wolno jeszcze wykonać (patrz prefetch)
        self._last_nominatim = 0.0
        self.timeout = timeout
        self.cache_path = Path(cache_path) if cache_path else None
        self._cache: dict[str, list] = {}
        self._names: dict[str, str] = {}              # "KRAJ|kod" -> nazwa miejscowości (osobny plik, obok współrzędnych)
        self._names_dirty = False
        self._dirty = False
        self._miss: dict[str, float] = {}
        self._health: dict[str, list] = {"zippopotam": [0, 0.0, False], "nominatim": [0, 0.0, False]}   # [porażki, wstrzymana do, po wstrzymaniu]
        self._lock = threading.Lock()
        self._nom_lock = threading.Lock()
        if self.cache_path and self.cache_path.exists():
            try:
                self._cache = json.loads(self.cache_path.read_text())
            except (OSError, ValueError):
                self._cache = {}
        npath = self._names_path()
        if npath and npath.exists():
            try:
                self._names = json.loads(npath.read_text())
            except (OSError, ValueError):
                self._names = {}

    def _names_path(self) -> Optional[Path]:
        return self.cache_path.with_name(self.cache_path.stem + "_names.json") if self.cache_path else None

    # ---- zdrowie usług
    def _service_ok(self, name: str) -> bool:
        return self.online and time.monotonic() >= self._health[name][1]

    @property
    def nominatim_ok(self) -> bool:
        return self._service_ok("nominatim")

    def _good(self, name: str) -> None:
        h = self._health[name]
        h[0], h[2] = 0, False

    def _failed(self, name: str, hard: bool = False) -> None:
        with self._lock:
            h = self._health[name]
            h[0] += 1
            if hard or h[0] >= 3 or h[2]:                 # po wstrzymaniu wystarczy jedna nieudana próba, by wstrzymać znowu
                h[0], h[1], h[2] = 0, time.monotonic() + self.PAUSE_S, True

    # ---- klucze i warianty kodu
    @staticmethod
    def _query_code(country: str, postal: str) -> str:
        cands = postal_candidates(country, postal)
        return cands[0] if cands else str(postal or "").strip().upper().replace(" ", "")

    def _key(self, country: str, postal: str) -> str:
        return f"{str(country).upper()}|{self._query_code(str(country).upper(), postal)}"

    def _cache_get(self, country: str, postal: str) -> Optional[list]:
        """Trafienie w cache pod kluczem głównym, pod innym wariantem zapisu kodu albo pod kluczem ze starszej wersji
        (kod bez spacji; GB: pierwszy człon) - dzięki temu zapisane wcześniej kody nie giną po zmianie formatu kluczy."""
        country = str(country).upper()
        keys = [self._key(country, postal)] + [f"{country}|{c}" for c in postal_candidates(country, postal)]
        legacy = str(postal or "").strip().upper()
        keys.append(f"{country}|{legacy.split()[0] if country == 'GB' and legacy.split() else legacy.replace(' ', '')}")
        for k in keys:
            if k in self._cache:
                return self._cache[k]
        return None

    # ---- zapytania do usług (pojedyncze próby)
    def _fetch(self, country: str, code: str) -> Optional[tuple[float, float]]:
        url = f"https://api.zippopotam.us/{country.lower()}/{urllib.parse.quote(code)}"
        for attempt in (1, 2):
            try:
                with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                    place = json.load(resp)["places"][0]
                self._good("zippopotam")
                return float(place["latitude"]), float(place["longitude"])
            except urllib.error.HTTPError as e:
                if e.code in (404, 400):          # usługa odpowiada, ale kodu nie zna
                    self._good("zippopotam")
                    return None
                self._failed("zippopotam", hard=e.code in (429, 502, 503, 504))   # przeciążona / odmawia
                return None
            except (KeyError, IndexError):
                self._good("zippopotam")
                return None
            except (urllib.error.URLError, OSError, ValueError):
                if attempt == 1 and not self._health["zippopotam"][2]:
                    time.sleep(0.2)               # jedna szybka ponowna próba przy chwilowym błędzie (nie tuż po wstrzymaniu)
                    continue
                self._failed("zippopotam")
                return None
        return None

    def _nominatim_query(self, params: dict) -> Optional[tuple[float, float]]:
        with self._nom_lock:                      # limit Nominatim: 1 zapytanie na sekundę, także między wątkami
            wait = 1.1 - (time.monotonic() - self._last_nominatim)
            if wait > 0:
                time.sleep(wait)
            self._last_nominatim = time.monotonic()
        q = urllib.parse.urlencode({"format": "jsonv2", "limit": 1, **params})
        req = urllib.request.Request(f"https://nominatim.openstreetmap.org/search?{q}",
                                     headers={"User-Agent": "UmoyaLogis-LTL/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=max(self.timeout, 10.0)) as resp:
                hits = json.load(resp)
            self._good("nominatim")
            return (float(hits[0]["lat"]), float(hits[0]["lon"])) if hits else None
        except urllib.error.HTTPError as e:
            self._failed("nominatim", hard=e.code in (403, 429, 502, 503, 504))
            return None
        except (KeyError, IndexError):
            self._good("nominatim")
            return None
        except (urllib.error.URLError, OSError, ValueError):
            self._failed("nominatim")
            return None

    def _fetch_nominatim(self, country: str, postal: str) -> Optional[tuple[float, float]]:
        """Drugie źródło (dokładniejsze dla kodów, których nie zna zippopotam)."""
        cands = postal_candidates(country, postal)
        text = cands[0] if (country.upper() in ("GB", "IE") and cands) else postal
        return self._nominatim_query({"postalcode": text, "countrycodes": country.lower()})

    def _fetch_nominatim_city(self, country: str, postal: str, city: str) -> Optional[tuple[float, float]]:
        """Gdy sam kod nie wystarcza: kod z miejscowością jako zwykłe wyszukiwanie."""
        return self._nominatim_query({"q": f"{postal} {city}".strip(), "countrycodes": country.lower()})

    # ---- przybliżenia
    def _prefix_guess(self, country: str, code: str) -> Optional[tuple[float, float]]:
        """Położenie z innych znanych kodów tego kraju o wspólnym początku (min. 2 znaki) - dużo lepsze niż środek kraju."""
        want = _alnum(code)
        best, pts = 1, []
        for key, (lat, lon) in self._cache.items():
            c, _, k = key.partition("|")
            if c != country:
                continue
            k = _alnum(k)
            n = 0
            while n < min(len(want), len(k)) and want[n] == k[n]:
                n += 1
            if n > best:
                best, pts = n, [(lat, lon)]
            elif n == best and n > 1:
                pts.append((lat, lon))
        if best < 2 or not pts:
            return None
        return float(np.mean([p[0] for p in pts])), float(np.mean([p[1] for p in pts]))

    # ---- wyszukiwanie jednego kodu
    def _zippo_lookup(self, country: str, postal: str) -> tuple[Optional[tuple[float, float]], bool]:
        """Zippopotam, kolejne warianty zapisu kodu. -> (trafienie | None, czy usługa jednoznacznie odpowiedziała 'nie znam')."""
        definitive = True
        for cand in postal_candidates(country, postal):
            if not re.fullmatch(r"[A-Z0-9\- ]{2,12}", cand):
                continue
            if not self._service_ok("zippopotam"):
                return None, False
            before = self._health["zippopotam"][:]
            hit = self._fetch(country, cand)
            if hit:
                return hit, True
            if self._health["zippopotam"][0] != 0 or self._health["zippopotam"][1] != before[1]:
                definitive = False                # to nie było „nie znam kodu”, tylko błąd usługi
        return None, definitive

    def locate(self, country: str, postal: str, city: str = "", trace: Optional[list] = None) -> tuple[float, float, str]:
        """(lat, lon, źródło). `trace`: lista, do której dopisujemy kolejne kroki (do diagnostyki w interfejsie)."""
        country = str(country or "").strip().upper()
        postal = str(postal or "").strip()
        key = self._key(country, postal)
        note = (lambda m: trace.append(m)) if trace is not None else (lambda m: None)
        cached = self._cache_get(country, postal)
        if cached is not None:
            lat, lon = cached
            note(f"cache: {key}")
            return lat, lon, "cache"
        missed = time.monotonic() - self._miss.get(key, -1e9) < self.MISS_TTL_S
        if missed:
            note("usługi online nie znały tego kodu (zapamiętane na godzinę)")
        elif self.online:
            hit, definitive = self._zippo_lookup(country, postal)
            note(f"zippopotam.us ({', '.join(postal_candidates(country, postal)) or '-'}): "
                 + ("trafienie" if hit else "nie znaleziono" if definitive else "usługa niedostępna / błąd"))
            definitive_all = definitive
            if not hit and self._service_ok("nominatim") and self.nominatim_budget > 0:
                self.nominatim_budget -= 1
                hit = self._fetch_nominatim(country, postal)
                note("Nominatim (kod): " + ("trafienie" if hit else "brak"))
                if not hit and city:
                    self.nominatim_budget -= 1
                    hit = self._fetch_nominatim_city(country, postal, city)
                    note(f"Nominatim (kod + {city}): " + ("trafienie" if hit else "brak"))
                definitive_all = definitive_all and self._service_ok("nominatim")
            elif not hit:
                definitive_all = False
                note("Nominatim pominięty (wstrzymany lub wyczerpany limit zapytań)")
            if hit:
                self._cache[key] = list(hit)
                self._dirty = True
                return hit[0], hit[1], "online"
            if definitive_all:
                self._miss[key] = time.monotonic()
        else:
            note("tryb offline (wyłączone ustalanie położenia przez internet)")
        guess = self._prefix_guess(country, self._query_code(country, postal))
        if guess:
            note("przybliżono z innych kodów o wspólnym początku")
            return guess[0], guess[1], "prefiks"
        lat, lon = COUNTRY_CENTROIDS.get(country, (50.0, 10.0))
        note("użyto środka kraju")
        return lat, lon, "kraj"

    # ---- nazwy miejscowości z kodów pocztowych
    def _fetch_place(self, country: str, code: str) -> Optional[str]:
        """Nazwa miejscowości dla kodu z zippopotam.us (None = nie znamy albo usługa niedostępna)."""
        url = f"https://api.zippopotam.us/{country.lower()}/{urllib.parse.quote(code)}"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                name = str(json.load(resp)["places"][0]["place name"]).strip()
            self._good("zippopotam")
            return name or None
        except urllib.error.HTTPError as e:
            if e.code in (404, 400):
                self._good("zippopotam")
            else:
                self._failed("zippopotam", hard=e.code in (429, 502, 503, 504))
        except (KeyError, IndexError, ValueError):
            self._good("zippopotam")
        except (urllib.error.URLError, OSError):
            self._failed("zippopotam")
        return None

    def _reverse_place(self, lat: float, lon: float) -> Optional[str]:
        """Miejscowość dla współrzędnych (Nominatim reverse) - zapasowe źródło, gdy zippopotam nie zna kodu."""
        with self._nom_lock:
            wait = 1.1 - (time.monotonic() - self._last_nominatim)
            if wait > 0:
                time.sleep(wait)
            self._last_nominatim = time.monotonic()
        q = urllib.parse.urlencode({"format": "jsonv2", "lat": lat, "lon": lon, "zoom": 10, "accept-language": "pl,en"})
        req = urllib.request.Request(f"https://nominatim.openstreetmap.org/reverse?{q}",
                                     headers={"User-Agent": "UmoyaLogis-LTL/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=max(self.timeout, 10.0)) as resp:
                addr = json.load(resp).get("address", {})
            self._good("nominatim")
            for k in ("city", "town", "village", "municipality", "county"):
                if addr.get(k):
                    return str(addr[k])
        except urllib.error.HTTPError as e:
            self._failed("nominatim", hard=e.code in (403, 429, 502, 503, 504))
        except (ValueError, KeyError):
            self._good("nominatim")
        except (urllib.error.URLError, OSError):
            self._failed("nominatim")
        return None

    def place_cached(self, country: str, postal: str) -> str:
        """Nazwa miejscowości z cache (bez zapytań do internetu); pusty tekst, gdy jeszcze nieznana."""
        country = str(country or "").strip().upper()
        keys = [self._key(country, postal)] + [f"{country}|{c}" for c in postal_candidates(country, postal)]
        return next((self._names[k] for k in keys if k in self._names), "")

    def place_name(self, country: str, postal: str) -> str:
        """Miejscowość odczytana z kodu pocztowego (cache -> zippopotam.us -> Nominatim reverse). Puste, gdy nie ustalono."""
        country = str(country or "").strip().upper()
        postal = str(postal or "").strip()
        if not country or not postal:
            return ""
        name = self.place_cached(country, postal)
        if name or not self.online:
            return name
        key = self._key(country, postal)
        if time.monotonic() - self._miss.get("N" + key, -1e9) < self.MISS_TTL_S:
            return ""
        if self._service_ok("zippopotam"):
            for code in postal_candidates(country, postal)[:3]:
                name = self._fetch_place(country, code)
                if name:
                    break
        if not name and self._service_ok("nominatim") and self.nominatim_budget > 0:
            cached = self._cache_get(country, postal)
            if cached is not None:
                self.nominatim_budget -= 1
                name = self._reverse_place(cached[0], cached[1]) or ""
        if name:
            self._names[key] = name
            self._names_dirty = True
            return name
        self._miss["N" + key] = time.monotonic()
        return ""

    def reset_failures(self) -> None:
        """Zdejmuje wstrzymanie usług i zapomina, które kody były „nieznane” - następne wyszukiwanie pyta od nowa."""
        with self._lock:
            self._miss.clear()
            for h in self._health.values():
                h[0], h[1], h[2] = 0, 0.0, False

    def set_manual(self, country: str, postal: str, lat: float, lon: float) -> None:
        """Ręcznie podane współrzędne kodu (zapamiętywane na dysku jak pozostałe)."""
        self._cache[self._key(str(country).upper(), postal)] = [float(lat), float(lon)]
        self._dirty = True
        self.save()

    def prefetch(self, pairs, progress=None, workers: int = 8, max_nominatim: int = 150) -> dict:
        """Szybkie, równoległe ustalanie położenia wielu kodów naraz (dla dużych wklejek).

        `pairs`: (kraj, kod) albo (kraj, kod, miejscowość).
        1. zippopotam.us w wielu wątkach, każdy kod w kilku wariantach zapisu (kody GB wg pierwszego członu,
           więc wiele ofert = jedno zapytanie),
        2. kody, których tam nie ma: Nominatim po kolei (limit 1/s), nie więcej niż `max_nominatim` zapytań;
           reszta dostanie przybliżenie z podobnych kodów albo środek kraju.
        Zwraca statystyki: {'cache', 'online', 'missing', 'total'}."""
        todo, seen = [], set()
        for item in pairs:
            country, postal = str(item[0]).strip().upper(), item[1]
            city = str(item[2]).strip() if len(item) > 2 and item[2] == item[2] and item[2] is not None else ""
            key = self._key(country, postal)
            if self._cache_get(country, postal) is None and key not in seen:
                seen.add(key)
                todo.append((country, str(postal), city, key))
        stats = {"cache": len(seen) == 0, "online": 0, "missing": 0, "total": len(todo)}
        done = 0

        def tick():
            nonlocal done
            done += 1
            if progress:
                progress(done, len(todo))

        missing = []
        now = time.monotonic()
        todo_net = [t for t in todo if now - self._miss.get(t[3], -1e9) >= self.MISS_TTL_S]
        skipped = [t for t in todo if t not in todo_net]
        if self.online and todo_net:
            def one(item):
                country, postal, _city, _key = item
                if not self._service_ok("zippopotam"):
                    return item, None, False
                hit, definitive = self._zippo_lookup(country, postal)
                return item, hit, definitive
            definitive_miss = {}
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for fut in as_completed([pool.submit(one, it) for it in todo_net]):
                    item, hit, definitive = fut.result()
                    if hit:
                        self._cache[item[3]] = list(hit)
                        self._dirty = True
                        stats["online"] += 1
                        tick()
                    else:
                        missing.append(item)
                        definitive_miss[item[3]] = definitive
        else:
            definitive_miss = {}
            missing = list(todo_net)
        stats["missing"] += 0
        self.nominatim_budget = max_nominatim
        for item in skipped:
            stats["missing"] += 1
            tick()
        for item in missing:
            hit = None
            if self.online and self._service_ok("nominatim") and self.nominatim_budget > 0:
                self.nominatim_budget -= 1
                hit = self._fetch_nominatim(item[0], item[1].strip())
                if not hit and item[2] and self.nominatim_budget > 0:
                    self.nominatim_budget -= 1
                    hit = self._fetch_nominatim_city(item[0], item[1].strip(), item[2])
            if hit:
                self._cache[item[3]] = list(hit)
                self._dirty = True
                stats["online"] += 1
            else:
                stats["missing"] += 1
                if definitive_miss.get(item[3]) and self._service_ok("nominatim"):
                    self._miss[item[3]] = time.monotonic()
            tick()
        self.save()
        return stats

    def save(self) -> None:
        npath = self._names_path()
        if npath and self._names_dirty:
            try:
                npath.parent.mkdir(parents=True, exist_ok=True)
                npath.write_text(json.dumps(self._names, ensure_ascii=False))
                self._names_dirty = False
            except OSError:
                pass
        if self.cache_path and self._dirty:
            try:
                self.cache_path.parent.mkdir(parents=True, exist_ok=True)
                self.cache_path.write_text(json.dumps(self._cache))
                self._dirty = False
            except OSError:
                pass


def add_coordinates(df: pd.DataFrame, geocoder: Geocoder) -> pd.DataFrame:
    """Dodaje start_lat/lon, end_lat/lon oraz geo_precise (czy oba punkty zostały ustalone z kodu pocztowego,
    a nie przybliżone z podobnych kodów lub środka kraju)."""
    df = df.copy()
    for side in ("start", "end"):
        cities = df[f"{side}_city"] if f"{side}_city" in df.columns else [""] * len(df)
        res = [geocoder.locate(c, p, str(t) if t == t and t is not None else "")
               for c, p, t in zip(df[f"{side}_country"], df[f"{side}_postal"], cities)]
        df[f"{side}_lat"] = [r[0] for r in res]
        df[f"{side}_lon"] = [r[1] for r in res]
        df[f"{side}_geo"] = [r[2] for r in res]
    df["geo_precise"] = (~df["start_geo"].isin(["kraj", "prefiks"])) & (~df["end_geo"].isin(["kraj", "prefiks"]))
    geocoder.save()
    return df
