# Wdrożenie na serwer biurowy (wersja początkowa)

Aplikacja działa w jednym kontenerze Docker. Użytkownicy otwierają ją w przeglądarce pod adresem serwera
(`http://ADRES_SERWERA:8501`), nic nie instalują. Dane (historia ofert, zapisane tabele i rankingi) są wspólne dla
wszystkich i leżą w katalogu `data/` na serwerze.

## 1. Wymagania na serwerze (Windows 11 Pro)

* **Docker Desktop** z włączonym WSL2 (https://www.docker.com/products/docker-desktop/).
* **Git** (lub pobranie repozytorium jako ZIP).
* Stały adres IP serwera w sieci biura (rezerwacja DHCP w routerze albo adres statyczny).

## 2. Pierwsze uruchomienie

**Najprościej (skrypt):** w PowerShellu jako administrator

```powershell
git clone --branch Optymalizator_tras_LTL_serwer4 https://github.com/DomTom23/UmoyaLogis.git C:\umoyalogis
cd C:\umoyalogis
Set-ExecutionPolicy -Scope Process Bypass -Force
.\deploy\install.ps1
```

Skrypt sprawdza Docker i Git, tworzy `.env` z losowym hasłem (wypisze je na ekranie), buduje i uruchamia aplikację,
czeka na gotowość, otwiera port w zaporze (tylko sieć prywatna), ustawia codzienną kopię zapasową i podaje adres dla
biura. Ten sam skrypt służy do aktualizacji. Dodatkowo `-Valhalla` uruchamia też własny serwer tras (sekcja 8).

**Ręcznie** (to samo krok po kroku):

W PowerShellu:

```powershell
git clone https://github.com/DomTom23/UmoyaLogis.git C:\umoyalogis
cd C:\umoyalogis
git checkout Optymalizator_tras_LTL_serwer4
copy .env.example .env
notepad .env                 # ustaw APP_PASSWORD (wspólne hasło) i ewentualnie VALHALLA_URL
docker compose up -d --build
```

Sprawdź: `docker compose ps` (stan `healthy`) i otwórz `http://localhost:8501`.

## 3. Dostęp z innych komputerów

1. Wpuść port w zaporze (PowerShell jako administrator):
   `New-NetFirewallRule -DisplayName "Umoyalogis" -Direction Inbound -Protocol TCP -LocalPort 8501 -Action Allow -Profile Private`
   (sieć biurowa musi być oznaczona w Windows jako „Prywatna”).
2. Na innym komputerze otwórz `http://ADRES_SERWERA:8501` i zaloguj się wspólnym hasłem z `.env`.
3. **Nie wystawiaj portu do internetu** (bez przekierowania portów w routerze). Hasło jest tylko osłoną sieci biurowej,
   a nie systemem kont.

## 4. Automatyczny start po restarcie

* Kontener ma `restart: unless-stopped`, więc wstaje razem z Dockerem.
* Docker Desktop: *Settings → General → Start Docker Desktop when you sign in*.
* Docker Desktop startuje dopiero po zalogowaniu użytkownika, więc dla serwera bez monitora włącz automatyczne
  logowanie na dedykowane konto (`netplwiz`) i w BIOS-ie „Restore on AC power loss: Power On” (po awarii zasilania).

## 5. Aktualizacja aplikacji

```powershell
cd C:\umoyalogis
git pull
docker compose up -d --build
```

Dane w `data\` zostają. Zmiana wersji trwa kilkanaście sekund, w tym czasie aplikacja jest niedostępna.

## 6. Kopie zapasowe

Kopia bazy (spójna także przy pracującej aplikacji), 30 ostatnich zostaje:

```powershell
docker compose exec -T app python scripts/backup_db.py /data/backups 30
```

Codzienne wykonanie o 2:00 (Harmonogram zadań):

```powershell
schtasks /Create /TN "Umoyalogis backup" /SC DAILY /ST 02:00 /TR "cmd /c cd /d C:\umoyalogis && docker compose exec -T app python scripts/backup_db.py /data/backups 30"
```

Kopie trafiają do `C:\umoyalogis\data\backups`. Skopiuj je okresowo na drugi dysk lub dysk sieciowy: lustro dysków
(RAID/Storage Spaces) chroni przed awarią dysku, ale nie przed skasowaniem danych.

**Przywracanie:** zatrzymaj aplikację (`docker compose stop`), podmień `data\umoyalogis.db` wybraną kopią
i uruchom (`docker compose start`).

## 7. Diagnostyka

* Logi: `docker compose logs -f app`.
* Restart: `docker compose restart app`.
* Port zajęty: zmień `APP_PORT` w `.env` i wykonaj `docker compose up -d`.
* Zmiana hasła lub adresu Valhalli: edytuj `.env`, potem `docker compose up -d`.

## 8. Co zawiera wersja początkowa i czego jeszcze nie

Jest: wszystkie funkcje aplikacji, wspólna baza dla biura, wspólne hasło, kopie zapasowe, start po restarcie.

Nie ma (kolejne kroki, w kolejności ważności):
1. **Własny serwer tras Valhalla** (instrukcja w sekcji 8). Publiczny serwer (`valhalla1.openstreetmap.de`) ma limit 1500 km na zapytanie
   (aplikacja dzieli trasy na odcinki), jest współdzielony i nie jest przeznaczony do codziennego ruchu kilku osób.
   Własny serwer wymaga zbudowania kafli dla Europy (kilkadziesiąt GB, wiele godzin) - warto go postawić od razu po
   uruchomieniu wersji początkowej.
2. **Konta użytkowników.** Jest jedno wspólne hasło, więc nie widać, kto co zapisał.
3. **Połączenie z API Trans.eu** (po otrzymaniu dokumentacji).
4. **HTTPS.** W sieci biurowej niepotrzebne; przy dostępie z zewnątrz użyj VPN, nie otwartego portu.

## 9. Praca wielu osób naraz

Każda osoba ma własną sesję (wklejone oferty, filtry, punkty na mapie), a wspólne są: baza (historia, zapisane tabele),
pamięć podręczna kodów pocztowych i tras. Wyszukiwanie na kilkuset ofertach potrafi zająć do ok. 30 s i obciąża jeden
rdzeń, więc przy kilku osobach naraz ostatnia może chwilę poczekać. Przy 8-10 osobach pracujących na zmianę to bez problemu.

## 8. Własny serwer tras (Valhalla)

Plik `docker-compose.valhalla.yml` dodaje do aplikacji własny serwer tras dla ciężarówek, bez limitu 1500 km na zapytanie
i bez współdzielenia z innymi użytkownikami:

```powershell
docker compose -f docker-compose.yml -f docker-compose.valhalla.yml up -d
```

(albo `.\deploy\install.ps1 -Valhalla`). Aplikacja łączy się z nim sama (`VALHALLA_URL=http://valhalla:8002`).

Co warto wiedzieć przed startem:

* **Pierwsze uruchomienie buduje kafle z map** pobranych z Geofabrik (lista krajów w pliku `docker-compose.valhalla.yml`).
  Dla ok. 12 krajów to kilkadziesiąt GB na dysku i od kilku do kilkunastu godzin pracy. W tym czasie aplikacja działa
  na publicznym serwerze tras albo w trybie linii prostych. Postęp: `docker compose logs -f valhalla`.
* **Pamięć.** Docker Desktop przydziela WSL2 domyślnie połowę RAM-u. Przy 16 GB RAM i budowie kafli dla wielu krajów
  ustaw w `C:\Users\<ty>\.wslconfig`: `[wsl2]` / `memory=12GB`, potem `wsl --shutdown` i restart Dockera. Jeśli budowa
  się wywala (brak pamięci), zmniejsz listę krajów albo buduj je po kilka.
* **Kafle lądują w `valhalla_data\`** (zostają po restarcie; mapy odświeżaj raz na kilka miesięcy: usuń ten katalog
  i uruchom ponownie).
* **Uwaga:** ten plik compose nie był przetestowany na prawdziwym Dockerze (środowisko, w którym powstał, nie miało
  Dockera). Jeśli obraz zgłosi nieznaną zmienną, sprawdź jej nazwę w opisie obrazu `gis-ops/docker-valhalla` i popraw
  plik.
* Czy działa: w aplikacji wyszukaj wariant i sprawdź, czy komunikat pod mapą mówi „Trasa po drogach dla ciężarówek
  (Valhalla…)” bez ostrzeżeń o odcinkach liczonych linią prostą.
