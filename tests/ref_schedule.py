"""Wzorzec: sync_windows sprzed optymalizacji (pętla po wierszach)."""
import pandas as pd


def _is_na(x) -> bool:
    return x is None or (isinstance(x, float) and x != x) or (x is pd.NaT) or (not isinstance(x, (str, bytes)) and pd.isna(x))



def sync_windows(df: pd.DataFrame) -> pd.DataFrame:
    """Po ręcznej zmianie daty w tabeli stare okno (z godzinami) przestaje pasować: wracamy do okna z samej daty."""
    df = df.copy()
    for side in ("load", "unload"):
        fcol, tcol, dcol = f"{side}_from", f"{side}_to", f"{side}_date"
        if fcol not in df.columns:
            continue
        for i in df.index:
            f, d = df.at[i, fcol], df.at[i, dcol]
            if not _is_na(f) and (_is_na(d) or pd.Timestamp(f).date() != pd.Timestamp(d).date()):
                if _is_na(d):
                    df.at[i, dcol] = pd.Timestamp(f).date()
                else:                                   # data zmieniona ręcznie: okno z godzinami jest nieaktualne
                    df.at[i, fcol] = pd.NaT
                    df.at[i, tcol] = pd.NaT
    return df


