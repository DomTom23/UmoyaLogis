# Optymalizator Tras LTL

Streamlit + Pandas + Folium. Wklejasz dane z Timocom / Trans.eu, aplikacja liczy koszty
i podpowiada najbardziej zyskowne doładunki (TOP 10).

```
pip install -r requirements.txt
streamlit run app.py
pytest
```

| Moduł | Zawartość |
|---|---|
| `ltl/parsers.py` | parsery Regex Timocom (tabulacje) i Trans.eu (bloki), domyślne LDM/waga, "Do negocjacji" |
| `ltl/geo.py` | współrzędne kodów (zippopotam.us + cache, fallback: środek kraju), haversine × 1,3 |
| `ltl/costs.py` | koszt dnia (9 h/dzień, 56 h/tydz.), paliwo, myto |
| `ltl/routing.py` | trasy po drogach dla ciężarówek: Valhalla (`truck`, bez klucza) lub OpenRouteService (HGV, klucz), fallback: linie proste |
| `ltl/route_edit.py` | punkty pośrednie (via) i przeliczanie kosztów po edycji trasy |
| `ltl/optimizer.py` | kombinacje do 4 zleceń, kontrola LDM/tonażu w każdym punkcie, limit odchylenia |
| `ltl/mapview.py`, `ltl/map_component/` | interaktywna mapa Leaflet (komponent Streamlit): zielone = załadunek, czerwone = rozładunek; przeciągnięcie fragmentu trasy dodaje punkt pośredni, przeciągnięcie punktu przesuwa go, dwuklik usuwa |

Odchylenie = dystans trasy − dystans najdłuższego zlecenia w kombinacji.
Offline kody pocztowe są mapowane na środek kraju, więc odległości w obrębie jednego kraju są wtedy zawyżone/zaniżone.

## Trasy rzeczywiste i edycja

Wybrany wariant jest liczony silnikiem Valhalla z profilem ciężarówki (parametry zestawu w panelu bocznym;
własny serwer: zmienna `VALHALLA_URL`, OpenRouteService: `ORS_API_KEY`). Po przeciągnięciu fragmentu trasy
aplikacja wyznacza trasę ponownie przez nowy punkt i przelicza dystans pełny/pusty, czas i koszty.
Gdy silnik tras jest nieosiągalny, pokazywana jest trasa orientacyjna (linie proste × 1,3) z ostrzeżeniem.
Źródło danych (Timocom / Trans.eu) jest zawsze wykrywane automatycznie.
