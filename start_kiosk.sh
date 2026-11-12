#!/usr/bin/env bash
# =====================================================================
# start_kiosk.sh - start the whole MediKiosk on the Pi with ONE command.
#
#   cd ~/Desktop/MediKiosk_hardware && bash start_kiosk.sh
#
# Opens one terminal window per program (so you can watch each log):
#   1 Agent      agent/kiosk_agent.py        the sensors + cameras
#   2 Edge       edge/edge_service.py        ML classification, saves each visit on the Pi
#   3 Sender     edge/sync_worker.py         sends saved visits to the staff dashboard (EMQX Cloud)
#   4 Backup     agent/save_readings.py      a CSV copy of every reading (~/medikiosk-backup)
#   5 Website    npm run dev -- --host       the kiosk screen (http://localhost:5173/kiosk)
# Mosquitto is NOT started here: it runs as a service on the Pi.
#
# Options:
#   --browser              also open the kiosk page full screen in Chromium
#   --with-local-backend   also start the OLD local backend (mediKiosk_Frontend/backend).
#                          The kiosk does not use it any more - only for old tests.
#
# Stop a program: Ctrl+C in its window. Stop everything: close the windows.
# =====================================================================

HW="$HOME/Desktop/MediKiosk_hardware"
FE="$HOME/Desktop/mediKiosk_Frontend"
SYNC_ENV="/etc/medikiosk/sync.env"
LOG_DIR="$HOME/medikiosk-logs"

OPEN_BROWSER=0
LOCAL_BACKEND=0
for arg in "$@"; do
  case "$arg" in
    --browser) OPEN_BROWSER=1 ;;
    --with-local-backend) LOCAL_BACKEND=1 ;;
    -h|--help) sed -n 2,22p "$0"; exit 0 ;;
    *) echo "unknown option: $arg (use --help)"; exit 1 ;;
  esac
done

say() { echo "[start_kiosk] $*"; }

# ---- checks before starting anything
[ -d "$HW" ] || { say "not found: $HW"; exit 1; }
[ -d "$FE" ] || { say "not found: $FE"; exit 1; }
[ -x "$HW/venv/bin/python3" ] || { say "hardware venv missing: $HW/venv (python3 -m venv venv)"; exit 1; }
if command -v systemctl >/dev/null && ! systemctl is-active --quiet mosquitto 2>/dev/null; then
  say "WARNING: Mosquitto is not running - start it with:  sudo systemctl start mosquitto"
fi
if pgrep -f "agent/kiosk_agent.py" >/dev/null; then
  say "the agent is ALREADY running - close the old windows first (two agents fight over the sensors)"
  exit 1
fi
# ---- the REAL kiosk: never fake sensors, never the simulation's kiosk ID
unset SIMULATE SIM_FAST MEDIKIOSK_SIM_DIR
export KIOSK_ID="K-01"
if [ -f "$FE/.env" ] && ! grep -Eq '^[[:space:]]*VITE_KIOSK_ID[[:space:]]*=[[:space:]]*K-01[[:space:]]*$' "$FE/.env"; then
  say "STOPPED: $FE/.env must say VITE_KIOSK_ID=K-01 on the real kiosk (found: $(grep -E '^[[:space:]]*VITE_KIOSK_ID' "$FE/.env" || echo 'nothing'))"
  exit 1
fi
if ! grep -q '"kiosk_id": "K-01"' "$HW/edge/edge_config.json"; then
  say "STOPPED: edge/edge_config.json must have \"kiosk_id\": \"K-01\" on the real kiosk"
  exit 1
fi
if [ ! -d "$FE/node_modules" ]; then
  say "STOPPED: the website's packages are missing - run:  cd $FE && npm install"
  exit 1
fi
if [ -f "$HW/hardware/calibration.json" ]; then
  say "note: hardware/calibration.json is used and OVERRIDES measurements.py: $(tr -d '\n ' < "$HW/hardware/calibration.json" | grep -oE '"(SENSOR_MOUNT_MM|TOF_SCALE|TEMP_OFFSET_C)":[-0-9.]+' | tr '\n' ' ')"
fi
mkdir -p "$LOG_DIR"

# ---- open one window per program (lxterminal on Raspberry Pi OS; falls back to any terminal,
#      or to a log file in ~/medikiosk-logs when there is no desktop, e.g. over SSH)
# REAL_ENV is put INSIDE every window's command: lxterminal opens new windows from its
# first (already running) copy, so a window does not always get this script's exports.
# `trap : USR1` keeps the window's bash alive when someone runs kill -USR1 on the agent
# (bash's default for USR1 is to quit, which would close the window and stop the agent).
REAL_ENV="unset SIMULATE SIM_FAST MEDIKIOSK_SIM_DIR; export KIOSK_ID=K-01"
open_window() {   # open_window "Title" "command"
  local title="$1" cmd="$REAL_ENV; $2"
  local wrapped="trap : USR1; echo '=== $title ==='; $cmd; echo; echo '[$title stopped] - press Enter to close this window'; read"
  if [ -n "$DISPLAY$WAYLAND_DISPLAY" ] && command -v lxterminal >/dev/null; then
    lxterminal --title="$title" -e bash -c "$wrapped" &
  elif [ -n "$DISPLAY$WAYLAND_DISPLAY" ] && command -v x-terminal-emulator >/dev/null; then
    x-terminal-emulator -T "$title" -e bash -c "$wrapped" &
  else
    local log
    log="$LOG_DIR/$(echo "$title" | tr ' A-Z' '_a-z').log"
    say "no desktop - running \"$title\" in the background, log: $log"
    nohup bash -c "trap : USR1; $cmd" >>"$log" 2>&1 &
  fi
}

VENV="cd '$HW' && source venv/bin/activate"

say "1/5 agent (wait for \"scale ESP32 connected\")"
open_window "1 Agent" "$VENV && python3 agent/kiosk_agent.py"
sleep 8   # the scale ESP32 restarts when its port opens (~8 s)

say "2/5 edge (ML classification)"
open_window "2 Edge" "$VENV && python3 edge/edge_service.py edge/edge_config.json"

if [ -r "$SYNC_ENV" ]; then
  say "3/5 sender (to the staff dashboard)"
  open_window "3 Sender" "$VENV && set -a && source '$SYNC_ENV' && set +a && python3 edge/sync_worker.py edge/edge_config.json"
elif [ -e "$SYNC_ENV" ]; then
  say "3/5 sender NOT started: $SYNC_ENV exists but this user can't read it"
  say "    fix: sudo chown root:$(id -gn) $SYNC_ENV && sudo chmod 640 $SYNC_ENV"
else
  say "3/5 sender NOT started: $SYNC_ENV is missing (EMQX Cloud login) - visits wait safely on the Pi"
  say "    set it up:  sudo mkdir -p /etc/medikiosk && sudo cp $HW/edge/sync.env.example $SYNC_ENV && sudo nano $SYNC_ENV"
fi

say "4/5 backup recorder (~/medikiosk-backup)"
open_window "4 Backup" "$VENV && python3 agent/save_readings.py"

if [ "$LOCAL_BACKEND" = 1 ]; then
  say "+   old local backend (--with-local-backend)"
  open_window "Old local backend" "cd '$FE/backend' && source .venv/bin/activate && python -m uvicorn app.main:app --host 0.0.0.0 --port 8000"
fi

say "5/5 website"
open_window "5 Website" "cd '$FE' && npm run dev -- --host"

if [ "$OPEN_BROWSER" = 1 ]; then
  sleep 6
  BROWSER=$(command -v chromium-browser || command -v chromium)
  if [ -n "$BROWSER" ]; then
    say "opening the kiosk page full screen (exit full screen: F11)"
    "$BROWSER" --start-fullscreen http://localhost:5173/kiosk >/dev/null 2>&1 &
  else
    say "Chromium not found - open http://localhost:5173/kiosk yourself"
  fi
else
  say "done - open http://localhost:5173/kiosk in ONE Chromium tab and press F11"
fi
