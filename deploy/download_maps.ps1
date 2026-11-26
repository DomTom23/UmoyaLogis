# Pobiera mapy OSM (Geofabrik) z wznawianiem i sprawdzeniem rozmiaru, a potem wklada je do wolumenu Valhalli.
# Uruchom w folderze projektu:  .\deploy\download_maps.ps1
# Wymaga: Docker Desktop uruchomiony, ok. 25 GB wolnego miejsca na dysku C: (tymczasowo, folder MapsDir).
param(
    [string]$MapsDir = "C:\valhalla_maps",
    [string[]]$Countries = @("poland","germany","france","belgium","netherlands","luxembourg","italy","austria","czech-republic","switzerland","spain","great-britain"),
    [switch]$KeepFiles,
    [switch]$MergeOnly      # mapy sa juz w wolumenie Valhalli: tylko scal w jeden plik i przebuduj kafle
)
$ErrorActionPreference = "Continue"
$compose = @("-f", "docker-compose.yml", "-f", "docker-compose.valhalla.yml")

function Step($t) { Write-Host ""; Write-Host "==> $t" -ForegroundColor Cyan }

Step "Zatrzymuje Valhalle"
docker compose @compose stop valhalla 2>&1 | Out-Null

if (-not $MergeOnly) {
    New-Item -ItemType Directory -Force -Path $MapsDir | Out-Null

    foreach ($c in $Countries) {
        $url  = "https://download.geofabrik.de/europe/$c/$c-latest.osm.pbf"
        $file = Join-Path $MapsDir "$c-latest.osm.pbf"
        Step "Mapa: $c"
        $expected = 0
        $head = curl.exe -s -L -I $url 2>$null | Select-String -Pattern "^Content-Length"
        if ($head) { $expected = [int64](($head | Select-Object -Last 1).ToString().Split(":")[1].Trim()) }
        if ($expected -le 0) { Write-Host "Nie udalo sie ustalic rozmiaru $url - pomijam. Sprawdz polaczenie." -ForegroundColor Red; continue }
        Write-Host ("Oczekiwany rozmiar: {0:N0} MB" -f ($expected / 1MB))
        for ($try = 1; $try -le 30; $try++) {
            $have = 0
            if (Test-Path $file) { $have = (Get-Item $file).Length }
            if ($have -eq $expected) { break }
            if ($have -gt $expected) { Remove-Item $file -Force }
            Write-Host ("Pobieranie (podejscie {0}), mam {1:N0} MB" -f $try, ($have / 1MB))
            curl.exe -s -L -C - --retry 5 --retry-delay 10 --retry-all-errors -o $file $url 2>$null
            Start-Sleep -Seconds 2
        }
        $have = 0
        if (Test-Path $file) { $have = (Get-Item $file).Length }
        if ($have -ne $expected) { Write-Host "BLAD: $c ma $have bajtow zamiast $expected. Uruchom skrypt jeszcze raz (wznowi)." -ForegroundColor Red; exit 1 }
        Write-Host "OK: $c" -ForegroundColor Green
    }

    Step "Czyszcze wolumen Valhalli i kopiuje mapy (kilka-kilkanascie minut)"
    docker run --rm -v umoyalogis_valhalla_data:/d alpine sh -c "rm -rf /d/*" 2>&1 | Out-Null
    docker run --rm -v "$($MapsDir):/src" -v umoyalogis_valhalla_data:/d alpine sh -c "cp /src/*.osm.pbf /d/ && ls -la /d" 2>&1

    if (-not $KeepFiles) {
        Step "Usuwam tymczasowe pliki z $MapsDir"
        Remove-Item "$MapsDir\*.osm.pbf" -Force
    }
}

Step "Usuwam stare, niepelne kafle (jesli sa)"
docker rm -f vtest 2>&1 | Out-Null
docker volume rm vtest 2>&1 | Out-Null
docker run --rm -v umoyalogis_valhalla_data:/d alpine sh -c "rm -rf /d/valhalla_tiles /d/valhalla_tiles.tar /d/admin_data /d/file_hashes.txt /d/duplicateways.txt" 2>&1 | Out-Null

Step "Scalam mapy w jeden plik europe.osm.pbf (osmium, 30-90 min, bez komunikatow)"
docker run --rm -v umoyalogis_valhalla_data:/d debian:stable-slim sh -c "apt-get update -qq && apt-get install -y -qq osmium-tool && cd /d && osmium merge *-latest.osm.pbf -o europe.osm.pbf --overwrite && rm -f /d/*-latest.osm.pbf && ls -la /d"
if ($LASTEXITCODE -ne 0) { Write-Host "BLAD scalania map. Wklej komunikat." -ForegroundColor Red; exit 1 }

Step "Uruchamiam Valhalle (budowa kafli z jednego pliku potrwa kilka godzin)"
docker compose @compose up -d valhalla 2>&1 | Out-Null
Write-Host "Gotowe. Postep: docker compose -f docker-compose.yml -f docker-compose.valhalla.yml logs --since 5m -f valhalla"
