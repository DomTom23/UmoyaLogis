# Dodaje kraje do dzialajacego serwera tras Valhalla BEZ jego wylaczania na czas budowy.
# Nowe kafle buduja sie w osobnym kontenerze (valhalla_build); serwer caly czas dziala na starych.
# Gdy budowa sie skonczy, -Finish podmienia kafle i restartuje serwer (kilka minut przerwy).
#
# 1) .\deploy\add_maps.ps1                      (domyslnie: denmark, slovakia, hungary; pobiera, scala, startuje budowe)
#    .\deploy\add_maps.ps1 -Countries denmark,sweden
# 2) .\deploy\add_maps.ps1 -Status              (jak idzie budowa)
# 3) .\deploy\add_maps.ps1 -Finish              (po 'Finished tarring': podmiana kafli i restart serwera)
# Wymaga: Docker Desktop uruchomiony, ok. 150 GB wolnego miejsca na dysku C: w trakcie budowy.
param(
    [string[]]$Countries = @("denmark", "slovakia", "hungary"),
    [string]$MapsDir = "C:\valhalla_maps",
    [switch]$Status,
    [switch]$Finish,
    [switch]$Force
)
$ErrorActionPreference = "Continue"
$compose = @("-f", "docker-compose.yml", "-f", "docker-compose.valhalla.yml")
$Live = "umoyalogis_valhalla_data"
$Build = "umoyalogis_valhalla_build"
$BuildName = "valhalla_build"
$Image = "ghcr.io/gis-ops/docker-valhalla/valhalla:latest"

function Step($t) { Write-Host ""; Write-Host "==> $t" -ForegroundColor Cyan }
function Fail($t) { Write-Host "BLAD: $t" -ForegroundColor Red; exit 1 }
function ContainerState($name) { return ((cmd /c "docker inspect -f {{.State.Status}} $name 2>&1") -join "").Trim() }
function BuildLog() { return @(cmd /c "docker logs $BuildName 2>&1") }

# ------------------------------------------------------------------ -Status
if ($Status) {
    Step "Budowa dodatkowych map"
    $st = ContainerState $BuildName
    if ($st -notin @("running", "exited")) { Write-Host "Brak budowy (kontener $BuildName nie istnieje)."; exit 0 }
    Write-Host "Kontener $BuildName : $st"
    $log = BuildLog
    $failed = @($log | Select-String -Pattern "Failed tile")
    $bad = @($log | Select-String -Pattern "Aborted|core dumped|unable to read blob|Killed")
    $tar = @($log | Select-String -Pattern "Finished tarring")
    $bld = @($log | Select-String -Pattern "Building [0-9]+ tiles")
    if ($bld.Count -gt 0) { Write-Host ("Ostatnia budowa: " + $bld[-1].Line.Trim()) }
    Write-Host ("Failed tile: " + $failed.Count + "   awarie: " + $bad.Count)
    if ($tar.Count -gt 0 -and $failed.Count -eq 0 -and $bad.Count -eq 0) {
        Write-Host ("GOTOWE: " + $tar[-1].Line.Trim()) -ForegroundColor Green
        Write-Host "Uruchom:  .\deploy\add_maps.ps1 -Finish"
    } elseif ($failed.Count -gt 0 -or $bad.Count -gt 0) {
        Write-Host "SA PROBLEMY - wklej mi ostatnie linie logu:" -ForegroundColor Red
        $log | Where-Object { $_ -notmatch "Exceeded max speed|invalid_argument" } | Select-Object -Last 8 | ForEach-Object { Write-Host ("  " + $_) }
    } else {
        Write-Host "Budowa trwa. Sprawdz ponownie pozniej." -ForegroundColor Yellow
    }
    exit 0
}

# ------------------------------------------------------------------ -Finish
if ($Finish) {
    Step "Sprawdzam, czy budowa sie zakonczyla"
    $st = ContainerState $BuildName
    if ($st -eq "running") { Fail "budowa jeszcze trwa. Sprawdz: .\deploy\add_maps.ps1 -Status" }
    if ($st -ne "exited") { Fail "nie ma kontenera $BuildName (czy uruchomiles najpierw .\deploy\add_maps.ps1 ?)" }
    $log = BuildLog
    $failed = @($log | Select-String -Pattern "Failed tile")
    $bad = @($log | Select-String -Pattern "Aborted|core dumped|unable to read blob|Killed")
    $tar = @($log | Select-String -Pattern "Finished tarring")
    if ($tar.Count -eq 0) { Fail "w logach budowy nie ma 'Finished tarring' - budowa sie nie powiodla" }
    Write-Host ($tar[-1].Line.Trim())
    if (($failed.Count -gt 0 -or $bad.Count -gt 0) -and -not $Force) { Fail "budowa miala bledy (Failed tile: $($failed.Count), awarie: $($bad.Count)). Wklej mi logi; wymus tylko swiadomie przez -Force." }

    Step "Zatrzymuje serwer tras i podmieniam kafle (kilka minut przerwy w routingu)"
    docker compose @compose stop valhalla 2>&1 | Out-Null
    $swap = "set -e; cp /b/valhalla_tiles.tar /d/valhalla_tiles.tar.new; cp /b/europe.osm.pbf /d/europe.osm.pbf.new; " +
            "rm -rf /d/admin_data /d/valhalla_tiles; cp -r /b/admin_data /d/admin_data; cp /b/file_hashes.txt /d/file_hashes.txt; " +
            "mv /d/valhalla_tiles.tar.new /d/valhalla_tiles.tar; mv /d/europe.osm.pbf.new /d/europe.osm.pbf; ls -la /d"
    docker run --rm -v "$($Build):/b:ro" -v "$($Live):/d" alpine sh -c $swap
    if ($LASTEXITCODE -ne 0) { Fail "podmiana kafli nie powiodla sie (serwer zatrzymany). Wklej komunikat; stary stan: docker compose up -d valhalla" }

    Step "Uruchamiam serwer tras"
    docker compose @compose up -d valhalla 2>&1 | Out-Null
    $ok = $false
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Seconds 5
        try { $s = Invoke-RestMethod -Uri "http://127.0.0.1:8002/status" -TimeoutSec 5; if ($s) { $ok = $true; break } } catch { }
    }
    if (-not $ok) { Fail "serwer nie odpowiada po 5 minutach. Sprawdz: docker logs valhalla --tail 30" }
    Write-Host "Serwer odpowiada." -ForegroundColor Green

    Step "Sprzatanie"
    docker rm $BuildName 2>&1 | Out-Null
    docker volume rm $Build 2>&1 | Out-Null
    Write-Host "Gotowe. Sprawdz trase przez nowe kraje, np. Kopenhaga - Budapeszt:"
    Write-Host '  $b = ''{"locations":[{"lat":55.68,"lon":12.57},{"lat":47.50,"lon":19.04}],"costing":"truck"}'''
    Write-Host '  (Invoke-RestMethod -Uri http://127.0.0.1:8002/route -Method Post -Body $b -ContentType "application/json").trip.summary'
    exit 0
}

# ------------------------------------------------------------------ start: pobranie, scalenie, budowa
Step "Sprawdzam stan"
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { Fail "nie znaleziono polecenia docker" }
docker run --rm -v "$($Live):/d:ro" alpine test -f /d/europe.osm.pbf
if ($LASTEXITCODE -ne 0) { Fail "w wolumenie $Live nie ma europe.osm.pbf (uzyj .\deploy\download_maps.ps1 -MergeOnly albo pelnego download_maps.ps1)" }
$existing = ContainerState $BuildName
if ($existing -in @("running", "exited", "created")) { Fail "budowa dodatkowych map juz istnieje (kontener $BuildName). Uzyj -Status albo -Finish." }
docker volume rm $Build 2>&1 | Out-Null

New-Item -ItemType Directory -Force -Path $MapsDir | Out-Null
foreach ($c in $Countries) {
    $file = Join-Path $MapsDir "$c-latest.osm.pbf"
    Step "Mapa: $c"
    $url = ""
    $expected = 0
    foreach ($cand in @("https://download.geofabrik.de/europe/$c/$c-latest.osm.pbf", "https://download.geofabrik.de/europe/$c-latest.osm.pbf")) {
        $head = curl.exe -s -L -I $cand 2>$null | Select-String -Pattern "^Content-Length"
        if ($head) { $size = [int64](($head | Select-Object -Last 1).ToString().Split(":")[1].Trim()) } else { $size = 0 }
        if ($size -gt 1000000) { $expected = $size; $url = $cand; break }
    }
    if ($expected -le 0) { Fail "nie znaleziono mapy '$c' w Geofabrik (sprawdz nazwe kraju, np. denmark, slovakia, hungary)" }
    Write-Host ("Adres: $url  (" + ("{0:N0}" -f ($expected / 1MB)) + " MB)")
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
    if ($have -ne $expected) { Fail "$c ma $have bajtow zamiast $expected. Uruchom skrypt jeszcze raz (wznowi pobieranie)." }
    Write-Host "OK: $c" -ForegroundColor Green
}

Step "Kopiuje nowe mapy do wolumenu budowy"
docker volume create $Build | Out-Null
foreach ($c in $Countries) {
    docker run --rm -v "$($MapsDir):/src:ro" -v "$($Build):/d" alpine cp "/src/$c-latest.osm.pbf" /d/
    if ($LASTEXITCODE -ne 0) { Fail "nie udalo sie skopiowac mapy $c" }
}

Step "Scalam stare mapy (europe.osm.pbf) z nowymi w jeden plik (osmium, kilkadziesiat minut)"
docker run --rm -v "$($Live):/live:ro" -v "$($Build):/d" debian:stable-slim sh -c "apt-get update -qq && apt-get install -y -qq osmium-tool && cd /d && osmium merge /live/europe.osm.pbf *-latest.osm.pbf -o europe.osm.pbf --overwrite && rm -f /d/*-latest.osm.pbf && ls -la /d"
if ($LASTEXITCODE -ne 0) { Fail "scalanie map nie powiodlo sie. Wklej komunikat." }

Step "Uruchamiam budowe kafli w osobnym kontenerze ($BuildName)"
docker run -d --name $BuildName --restart no -v "$($Build):/custom_files" `
    -e use_tiles_ignore_pbf=True -e build_elevation=False -e build_admins=True -e build_time_zones=False `
    -e serve_tiles=False -e server_threads=6 -e service_limits_truck_max_distance=5000000 `
    -e service_limits_max_exclude_polygons_length=100000 $Image
if ($LASTEXITCODE -ne 0) { Fail "nie udalo sie uruchomic kontenera budowy" }

Step "Sprzatanie plikow tymczasowych"
foreach ($c in $Countries) { Remove-Item (Join-Path $MapsDir "$c-latest.osm.pbf") -Force -ErrorAction SilentlyContinue }

Write-Host ""
Write-Host "Budowa ruszyla. Serwer tras dziala caly czas na starych kafelkach." -ForegroundColor Green
Write-Host "Trwa kilkanascie godzin (komputer ma byc wlaczony, bez usypiania). Sprawdzaj:  .\deploy\add_maps.ps1 -Status"
Write-Host "Gdy bedzie GOTOWE:  .\deploy\add_maps.ps1 -Finish"
