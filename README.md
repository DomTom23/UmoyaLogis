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

## Wczytywanie danych

Dane można wkleić i/lub dodać jako pliki (txt, csv, tsv, xlsx) - wiele źródeł naraz, każde w swoim
formacie (Timocom, Trans.eu, także kopia z polami w osobnych liniach). Rozpoznane zlecenia widać w edytowalnej
tabeli: popraw błędnie odczytane pola, uzupełnij ceny, dodaj lub usuń wiersze - zmiany od razu wchodzą do optymalizacji.

## Interfejs

Strona prowadzi krok po kroku: **1 Dane** (wklej / pliki / przykład, szukanie przyciskiem „Szukaj doładunków” -
bez skrótów klawiszowych) → **2 Zlecenia** (edytowalna tabela) → **3 Najlepsze doładunki** → **4 Trasa i koszty**
(mapa z edycją). Ustawienia (koszty, limity wyszukiwania, routing) są w panelu bocznym i działają od razu.

## Ranking po drogach, Szwajcaria, przeprawy, tunele

* Warianty są od razu liczone po rzeczywistych trasach (najlepsi kandydaci wg szacunku, równolegle, z cache odcinków)
  i sortowani po rzeczywistym zysku. Liczba zleceń w wariancie nie jest ograniczona - limitem jest naczepa
  (13,6 LDM / 24 t sprawdzane w każdym punkcie trasy); wyszukiwanie wiązkowe dokłada kolejne zlecenia.
* Szwajcaria jest omijana: próba `exclude_polygons` (własny serwer Valhalla), a gdy serwer tego nie obsługuje -
  wykrycie przejazdu i objazd przez punkty pośrednie (Lyon, Mediolan, Monachium…). Gdy się nie da, trasa jest oznaczona.
* Informacje o trasie: Eurotunel / prom przez La Manche, inne promy, płatne tunele alpejskie (Mont Blanc, Fréjus,
  Tauern, Katschberg, Arlberg, Karawanki…). Opłaty (opcjonalne, domyślnie 0) ustawia się w panelu bocznym.
  Wykrywanie opiera się na przebiegu trasy i przybliżonych współrzędnych - to ostrzeżenie, nie gwarancja.
