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
| `ltl/optimizer.py` | kombinacje do 4 zleceń, kontrola LDM/tonażu w każdym punkcie, limit odchylenia |
| `ltl/mapview.py` | mapa Folium (zielone = załadunek, czerwone = rozładunek) |

Odchylenie = dystans trasy − dystans najdłuższego zlecenia w kombinacji.
Offline kody pocztowe są mapowane na środek kraju, więc odległości w obrębie jednego kraju są wtedy zawyżone/zaniżone.
