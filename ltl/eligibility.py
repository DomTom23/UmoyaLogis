"""Które oferty nadają się dla naszego zestawu (ciągnik z naczepą): pojazd, nadwozie, ADR, winda, FTL."""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

DEFAULT_BODIES = ("firanka", "standard", "mega")
ALL_BODIES = ("firanka", "standard", "mega", "jumbo", "plandeka", "chłodnia", "izoterma", "skrzynia",
              "sztywna zabudowa", "walkingfloor", "coilmulde", "platforma", "niskopodwoziowy", "inne")


@dataclass(frozen=True)
class FleetProfile:
    semi_trailer_only: bool = True               # oferty tylko dla busa / solo (bez "z naczepą") odpadają
    accepted_bodies: tuple = DEFAULT_BODIES      # oferta musi dopuszczać choć jedno z tych nadwozi
    adr: bool = False                            # mamy uprawnienia ADR
    tail_lift: bool = False                      # mamy windę załadowczą
    allow_ftl: bool = True                       # FTL = cały pojazd dla jednego zleceniodawcy


def _body_ok(body: str, accepted: tuple) -> bool:
    toks = [t.split("(")[0].strip().lower() for t in body.split(",") if t.strip()]
    return not toks or any(t in accepted for t in toks)


def check_eligibility(df: pd.DataFrame, fleet: FleetProfile) -> pd.Series:
    """Powód wykluczenia dla każdej oferty ('' = nadaje się)."""
    reasons = []
    for r in df.itertuples():
        why = []
        if getattr(r, "status", "") == "Za duże dla naczepy":
            why.append("za duże dla naczepy")
        veh = (getattr(r, "vehicles", "") or "")
        if fleet.semi_trailer_only and veh and "z naczepą" not in veh:
            why.append("pojazd bez naczepy")
        body = (getattr(r, "body", "") or "")
        if not _body_ok(body, fleet.accepted_bodies):
            why.append("nadwozie: " + body.split(",")[0].split("(")[0].strip())
        extras = (getattr(r, "extras", "") or "")
        if "ADR" in extras and not fleet.adr:
            why.append("ADR")
        if "Winda" in extras and not fleet.tail_lift:
            why.append("wymaga windy")
        if not fleet.allow_ftl and (getattr(r, "load_type", "") or "") == "FTL":
            why.append("FTL")
        reasons.append(", ".join(why))
    return pd.Series(reasons, index=df.index)
