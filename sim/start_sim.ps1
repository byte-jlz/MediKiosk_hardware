# =====================================================================
# start_sim.ps1 - run the WHOLE kiosk on this laptop, like the Pi does,
# with fake sensors - and delete everything it made when you stop it.
#
#   cd C:\Users\User\Desktop\MediKiosk_hardware
#   powershell -ExecutionPolicy Bypass -File sim\start_sim.ps1
#
# Opens one window per program (same programs as start_kiosk.sh on the Pi):
#   Mosquitto, 1 Agent (SIMULATE=1), 2 Edge, 3 Sender, 4 Backup, 5 Website
#
# It can NOT affect the real kiosk on the Pi:
#   - kiosk ID K-SIM (the Pi is K-01): its own MQTT topics, its own EMQX login,
#     its own client ID - it never kicks the Pi off EMQX
#   - its own database + backup folder: C:\Users\User\medikiosk-sim
#   - its own Mosquitto on this laptop (localhost) - nothing goes to the Pi
#
# When you press Enter in THIS window, it stops every window it opened and
# runs sim\sim_cleanup.py: deletes C:\Users\User\medikiosk-sim and (if you
# give MONGO_URI) the K-SIM visits in MongoDB. The Pi's K-01 data is never touched.
#
# Options:  -Fast        fake readings take seconds instead of the real time
#           -KeepData    stop the windows but do NOT delete anything
#
# One-time: register kiosk K-SIM in the staff console; add an EMQX login K-SIM;
#           in medi-kiosk-frontend\.env set  VITE_KIOSK_ID=K-SIM
# =====================================================================
param([switch]$Fast, [switch]$KeepData)

$ErrorActionPreference = "Stop"
$Kiosk  = "K-SIM"
$HW     = Split-Path -Parent $PSScriptRoot                     # ...\MediKiosk_hardware
$FE     = Join-Path $env:USERPROFILE "Desktop\medi-kiosk-frontend"
$SimDir = Join-Path $env:USERPROFILE "medikiosk-sim"
$Py     = Join-Path $HW "venv\Scripts\python.exe"
$MosquittoExe  = "C:\Program Files\mosquitto\mosquitto.exe"
$MosquittoConf = Join-Path $FE "backend\mosquitto\medikiosk.conf"

function Say($msg) { Write-Host "[start_sim] $msg" }
function Stop-Sim($msg) { Write-Host "[start_sim] $msg" -ForegroundColor Red; exit 1 }

# ---------------- checks: refuse anything that could mix with the real kiosk
if ($env:OS -ne "Windows_NT") { Stop-Sim "this simulation is for the Windows laptop only - on the Pi use start_kiosk.sh" }
if (-not (Test-Path $Py)) { Stop-Sim "hardware venv not found: $Py" }
if (-not (Test-Path $FE)) { Stop-Sim "frontend folder not found: $FE" }
$envFile = Join-Path $FE ".env"
$feKiosk = $null
if (Test-Path $envFile) {
    $line = Select-String -Path $envFile -Pattern '^\s*VITE_KIOSK_ID\s*=\s*(\S+)' | Select-Object -First 1
    if ($line) { $feKiosk = $line.Matches[0].Groups[1].Value }
}
if ($feKiosk -ne $Kiosk) {
    Stop-Sim "medi-kiosk-frontend\.env must say VITE_KIOSK_ID=$Kiosk (it says '$feKiosk'). Fix it, then run this again."
}
$running = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
           Where-Object { $_.CommandLine -match 'kiosk_agent\.py|edge_service\.py|sync_worker\.py|save_readings\.py' }
if ($running) { Stop-Sim "kiosk programs are already running on this laptop - close those windows first." }

New-Item -ItemType Directory -Force -Path $SimDir | Out-Null

# ---------------- settings every window inherits (only for this simulation)
$env:KIOSK_ID = $Kiosk
$env:SIMULATE = "1"
if ($Fast) { $env:SIM_FAST = "1" } else { Remove-Item Env:SIM_FAST -ErrorAction SilentlyContinue }
$env:LIVE = "0"                                                # no idle sensor feed needed
$env:BACKUP_DIR = Join-Path $SimDir "backup"
$env:MEDIKIOSK_SIM_DIR = $SimDir
$env:PYTHONUNBUFFERED = "1"

# EMQX login for the Sender (K-SIM). Leave the address empty to skip the Sender.
Say "EMQX Cloud for the Sender - press Enter to skip (visits then stay on the laptop)"
$brokerHost = Read-Host "  EMQX address (xxxx.emqxsl.com)"
$startSender = $false
if ($brokerHost) {
    $sec = Read-Host "  password of the EMQX login $Kiosk" -AsSecureString
    $env:MEDIKIOSK_BROKER_HOST = $brokerHost.Trim()
    $env:MEDIKIOSK_BROKER_PORT = "8883"
    $env:MEDIKIOSK_BROKER_TLS = "1"
    $env:MEDIKIOSK_BROKER_USERNAME = $Kiosk
    $env:MEDIKIOSK_BROKER_PASSWORD = [System.Net.NetworkCredential]::new("", $sec).Password
    $startSender = $true
}

# ---------------- open the windows
$procs = @()
function Open-Window($title, $workDir, $command) {
    $full = "`$Host.UI.RawUI.WindowTitle = '$title'; Set-Location '$workDir'; Write-Host '=== $title ==='; $command"
    $p = Start-Process powershell -PassThru -ArgumentList @("-NoExit", "-ExecutionPolicy", "Bypass", "-Command", $full)
    $script:procs += $p
    Say "started $title"
}

$listening = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | ForEach-Object { $_.LocalPort })
if ($listening -contains 1883) {
    if ($listening -contains 9001) { Say "Mosquitto is already running (1883 + 9001) - using it" }
    else { Stop-Sim "port 1883 is taken but 9001 (websockets) is not - run 'net stop mosquitto' in an ADMIN PowerShell, then try again" }
} else {
    if (-not (Test-Path $MosquittoExe)) { Stop-Sim "Mosquitto not found: $MosquittoExe" }
    Open-Window "Mosquitto (simulation)" $HW "& '$MosquittoExe' -c '$MosquittoConf' -v"
    Start-Sleep -Seconds 2
}

Open-Window "1 Agent (SIMULATED sensors, $Kiosk)" $HW "& '$Py' agent\kiosk_agent.py"
Start-Sleep -Seconds 3
Open-Window "2 Edge ($Kiosk)" $HW "& '$Py' edge\edge_service.py edge\edge_config.sim.json"
if ($startSender) {
    Open-Window "3 Sender ($Kiosk -> EMQX)" $HW "& '$Py' edge\sync_worker.py edge\edge_config.sim.json"
} else {
    Say "3 Sender NOT started (no EMQX address) - visits stay in $SimDir"
}
Open-Window "4 Backup ($Kiosk)" $HW "& '$Py' agent\save_readings.py"
Open-Window "5 Website" $FE "npm run dev"

# the password is only needed by the Sender window, which already has it
Remove-Item Env:MEDIKIOSK_BROKER_PASSWORD -ErrorAction SilentlyContinue

Say ""
Say "SIMULATION RUNNING as kiosk $Kiosk - open http://localhost:5173/kiosk"
Say "Data goes to $SimDir (and to the staff dashboard as $Kiosk if the Sender runs)."
Say ""
Read-Host "[start_sim] press Enter here to STOP the simulation" | Out-Null

# ---------------- stop every window this script opened (and their child programs)
$ErrorActionPreference = "Continue"                             # a window that is already closed is fine
foreach ($p in $procs) {
    if (-not $p.HasExited) { & taskkill /PID $p.Id /T /F 2>$null | Out-Null }
}
Say "all simulation windows stopped"
Start-Sleep -Seconds 2                                          # let Windows release the database file

if ($KeepData) {
    Say "-KeepData: nothing deleted (data is in $SimDir). Clean up later: & '$Py' sim\sim_cleanup.py"
    exit 0
}

# ---------------- delete what the simulation made
Say "Paste MONGO_URI to also delete the $Kiosk visits in MongoDB (Enter = only the laptop folder)"
$secUri = Read-Host "  MONGO_URI" -AsSecureString
$uri = [System.Net.NetworkCredential]::new("", $secUri).Password
if ($uri) {
    $env:MONGO_URI = $uri
    if (-not $env:MONGO_DB) { $env:MONGO_DB = Read-Host "  MONGO_DB (Enter = db-test)"; if (-not $env:MONGO_DB) { $env:MONGO_DB = "db-test" } }
    & $Py (Join-Path $HW "sim\sim_cleanup.py")
    Remove-Item Env:MONGO_URI -ErrorAction SilentlyContinue
} else {
    & $Py (Join-Path $HW "sim\sim_cleanup.py") --local-only
}
