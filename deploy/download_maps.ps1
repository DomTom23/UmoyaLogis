# Pobiera mapy OSM (Geofabrik) z wznawianiem i sprawdzeniem rozmiaru, a potem wklada je do wolumenu Valhalli.
# Uruchom w folderze projektu:  .\deploy\download_maps.ps1
# Wymaga: Docker Desktop uruchomiony, ok. 25 GB wolnego miejsca na dysku C: (tymczasowo, folder MapsDir).
param(
    [string]$MapsDir = "C:\valhalla_maps",
    [string[]]$Countries = @("poland","germany","france","belgium","netherlands","luxembourg","italy","austria","czech-republic","switzerland","spain","great-britain"),
    [switch]$KeepFiles
)
$ErrorActionPreference = "Continue"
$compose = @("-f", "docker-compose.yml", "-f", "docker-compose.valhalla.yml")

function Step($t) { Write-Host ""; Write-Host "==> $t" -ForegroundColor Cyan }

Step "Zatrzymuje Valhalle"
docker compose @compose stop valhalla 2>&1 | Out-Null

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

Step "Uruchamiam Valhalle (budowa kafli potrwa kilka godzin)"
docker compose @compose up -d valhalla 2>&1 | Out-Null
Write-Host "Gotowe. Postep: docker compose -f docker-compose.yml -f docker-compose.valhalla.yml logs --since 5m -f valhalla"
