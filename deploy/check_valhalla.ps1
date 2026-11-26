# Sprawdza, czy budowa kafli Valhalli sie udala: bledy "Failed tile", koniec budowy, status serwera i test trasy.
# Uzycie (w folderze projektu):  .\deploy\check_valhalla.ps1            (logi z ostatnich 12 godzin)
#                                .\deploy\check_valhalla.ps1 -Since 48h
param([string]$Since = "12h", [string]$Url = "http://127.0.0.1:8002")
$ErrorActionPreference = "Continue"

function Head($t) { Write-Host ""; Write-Host "==> $t" -ForegroundColor Cyan }
function Ok($t)   { Write-Host "OK    $t" -ForegroundColor Green }
function Bad($t)  { Write-Host "BLAD  $t" -ForegroundColor Red }
function Info($t) { Write-Host "      $t" }

Head "Kontener"
$state = (cmd /c "docker inspect -f {{.State.Status}} valhalla 2>&1") -join ""
$restarts = (cmd /c "docker inspect -f {{.RestartCount}} valhalla 2>&1") -join ""
if ($state -eq "running") { Ok "kontener valhalla dziala (restartow: $restarts)" } else { Bad "kontener valhalla: $state" }
if ([int]("0" + ($restarts -replace "[^0-9]", "")) -gt 3) { Bad "kontener restartowal sie wielokrotnie - budowa sie wywala (sprawdz 'Aborted' ponizej)" }

Head "Logi z ostatnich $Since"
$log = @(cmd /c "docker logs valhalla --since $Since 2>&1")
Info ("linii w logach: " + $log.Count)
$failed  = @($log | Select-String -Pattern "Failed tile")
$aborted = @($log | Select-String -Pattern "Aborted|core dumped|unable to read blob|out of memory|Killed")
$build   = @($log | Select-String -Pattern "Building [0-9]+ tiles")
$tar     = @($log | Select-String -Pattern "Finished tarring")
if ($failed.Count -eq 0) { Ok "brak bledow 'Failed tile'" } else {
    Bad ("'Failed tile': " + $failed.Count)
    $failed | Select-Object -First 8 | ForEach-Object { Info $_.Line.Trim() }
}
if ($aborted.Count -eq 0) { Ok "brak 'Aborted' / 'unable to read blob' / braku pamieci" } else {
    Bad ("awarie budowy: " + $aborted.Count)
    $aborted | Select-Object -First 5 | ForEach-Object { Info $_.Line.Trim() }
}
if ($build.Count -gt 0) { Info ("ostatnia budowa: " + $build[-1].Line.Trim()) } else { Info "w tym oknie czasu nie widac startu budowy kafli (zwieksz -Since)" }
if ($tar.Count -gt 0) { Ok ("budowa zakonczona: " + $tar[-1].Line.Trim()) } else { Info "brak 'Finished tarring' - budowa trwa albo sie nie zakonczyla" }
$last = @($log | Where-Object { $_ -notmatch "Exceeded max speed|invalid_argument" } | Select-Object -Last 3)
Info "ostatnie linie logu:"
$last | ForEach-Object { Info ("  " + ([string]$_).Trim()) }

Head "Serwer"
$status = $null
try { $status = Invoke-RestMethod -Uri "$Url/status" -TimeoutSec 10 } catch { }
if ($status) { Ok ("serwer odpowiada, wersja Valhalli " + $status.version) } else { Bad "serwer nie odpowiada na $Url/status (budowa trwa albo kontener nie dziala)" }

Head "Test trasy ciezarowka: Warszawa - Madryt (ok. 2400 km)"
$routeOk = $false
if ($status) {
    $body = '{"locations":[{"lat":52.23,"lon":21.01},{"lat":40.42,"lon":-3.70}],"costing":"truck"}'
    try {
        $r = Invoke-RestMethod -Uri "$Url/route" -Method Post -Body $body -ContentType "application/json" -TimeoutSec 60
        $km = [math]::Round($r.trip.summary.length)
        if ($km -gt 1800 -and $km -lt 3200) { Ok "trasa policzona: $km km" ; $routeOk = $true } else { Bad "dziwna dlugosc trasy: $km km" }
    } catch {
        Bad ("trasa nie policzona: " + $_.Exception.Message)
    }
} else { Info "pominieto (serwer nie odpowiada)" }

Head "Wynik"
if ($failed.Count -eq 0 -and $aborted.Count -eq 0 -and $routeOk) {
    Write-Host "WSZYSTKO OK - serwer tras jest gotowy." -ForegroundColor Green
} elseif ($tar.Count -eq 0 -and $status -eq $null -and $failed.Count -eq 0 -and $aborted.Count -eq 0) {
    Write-Host "Budowa jeszcze trwa - sprawdz ponownie za godzine." -ForegroundColor Yellow
} else {
    Write-Host "SA PROBLEMY - wklej mi caly wynik tego skryptu." -ForegroundColor Red
}
