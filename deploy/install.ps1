<#
 Instalacja / aktualizacja Optymalizatora Tras LTL na serwerze Windows 11 (Docker Desktop).
 Uruchom w PowerShellu JAKO ADMINISTRATOR:
     Set-ExecutionPolicy -Scope Process Bypass -Force
     .\deploy\install.ps1                      # pierwsza instalacja z klonowaniem repozytorium
 lub, jeśli repozytorium już masz:   cd C:\umoyalogis ; .\deploy\install.ps1
 Parametry: -InstallDir C:\umoyalogis  -Port 8501  -Branch Optymalizator_tras_LTL_serwer4  -Valhalla
#>
param(
    [string]$InstallDir = "C:\umoyalogis",
    [int]$Port = 8501,
    [string]$Repo = "https://github.com/DomTom23/UmoyaLogis.git",
    [string]$Branch = "Optymalizator_tras_LTL_serwer4",
    [switch]$Valhalla
)
$ErrorActionPreference = "Stop"

function Step($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Fail($m) { Write-Host "BŁĄD: $m" -ForegroundColor Red; exit 1 }

Step "Sprawdzanie wymagań"
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) { Fail "Uruchom PowerShell jako administrator." }
$hasGit = [bool](Get-Command git -ErrorAction SilentlyContinue)
$inRepo = (Test-Path "docker-compose.yml") -and (Test-Path "deploy\install.ps1")
if (-not $hasGit -and -not $inRepo) { Fail "Brak Git. Zainstaluj: https://git-scm.com/download/win (albo uruchom skrypt z katalogu projektu pobranego przez GitHub Desktop)." }
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { Fail "Brak Dockera. Zainstaluj Docker Desktop: https://www.docker.com/products/docker-desktop/" }
docker info *> $null
if ($LASTEXITCODE -ne 0) { Fail "Docker nie odpowiada. Uruchom Docker Desktop, poczekaj aż pokaże 'Running', i spróbuj ponownie." }

if ($inRepo) {
    # skrypt uruchomiony w katalogu projektu (np. sklonowanym przez GitHub Desktop): używamy go bez klonowania
    $InstallDir = (Get-Location).Path
    Step "Używam bieżącego katalogu: $InstallDir"
    if ($hasGit -and (Test-Path ".git")) {
        git fetch origin $Branch 2>$null
        git checkout $Branch 2>$null
        git pull origin $Branch 2>$null
    } else {
        Write-Host "Brak Gita w PowerShellu - pomijam pobieranie zmian (zaktualizuj projekt w GitHub Desktop: Fetch/Pull na gałęzi $Branch)."
    }
} else {
    Step "Pobieranie programu do $InstallDir"
    if (Test-Path (Join-Path $InstallDir ".git")) {
        git -C $InstallDir fetch origin $Branch
        git -C $InstallDir checkout $Branch
        git -C $InstallDir pull origin $Branch
    } else {
        git clone --branch $Branch $Repo $InstallDir
    }
}
Set-Location $InstallDir

Step "Plik .env (hasło i port)"
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    $chars = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789".ToCharArray()
    $secret = -join (1..12 | ForEach-Object { $chars | Get-Random })
    (Get-Content ".env") -replace "^APP_PASSWORD=.*", "APP_PASSWORD=$secret" -replace "^APP_PORT=.*", "APP_PORT=$Port" | Set-Content ".env" -Encoding UTF8
    Write-Host "Utworzono .env. WSPÓLNE HASŁO DO APLIKACJI: $secret  (zapisz je; zmienisz je w pliku .env)" -ForegroundColor Yellow
} else {
    Write-Host ".env już istnieje - zostaje bez zmian."
}

Step "Budowanie i uruchamianie (pierwszy raz trwa kilka minut)"
$compose = @("-f", "docker-compose.yml")
if ($Valhalla) { $compose += @("-f", "docker-compose.valhalla.yml") }
docker compose @compose up -d --build
if ($LASTEXITCODE -ne 0) { Fail "docker compose nie powiodło się - patrz komunikaty powyżej." }

Step "Czekam na gotowość aplikacji"
$ok = $false
foreach ($i in 1..40) {
    try {
        $r = Invoke-WebRequest -UseBasicParsing "http://localhost:$Port/_stcore/health" -TimeoutSec 3
        if ($r.StatusCode -eq 200) { $ok = $true; break }
    } catch { }
    Start-Sleep -Seconds 3
}
if (-not $ok) { Fail "Aplikacja nie odpowiada. Zobacz logi: docker compose logs app" }

Step "Zapora Windows (tylko sieć prywatna)"
$rule = "Umoyalogis $Port"
if (-not (Get-NetFirewallRule -DisplayName $rule -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName $rule -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow -Profile Private | Out-Null
    Write-Host "Dodano regułę zapory dla portu $Port (profil: Prywatna)."
} else { Write-Host "Reguła zapory już istnieje." }

Step "Codzienna kopia zapasowa bazy (2:00)"
$task = "Umoyalogis backup"
$cmd = "cmd /c cd /d `"$InstallDir`" && docker compose exec -T app python scripts/backup_db.py /data/backups 30"
schtasks /Create /TN $task /SC DAILY /ST 02:00 /TR $cmd /F | Out-Null
Write-Host "Zadanie '$task' ustawione (kopie w $InstallDir\data\backups)."

Step "Gotowe"
$ip = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike "169.*" -and $_.IPAddress -ne "127.0.0.1" -and $_.PrefixOrigin -ne "WellKnown" } | Select-Object -First 1).IPAddress
Write-Host "Aplikacja działa. Na tym komputerze:  http://localhost:$Port" -ForegroundColor Green
if ($ip) { Write-Host "Z innych komputerów w biurze:        http://${ip}:$Port   (nadaj serwerowi stały adres IP w routerze)" -ForegroundColor Green }
Write-Host "Logi: docker compose logs -f app   |   Aktualizacja: ponownie uruchom ten skrypt"
