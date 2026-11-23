#!/usr/bin/env bash
# =====================================================================
# kiosk_control.sh - stop / restart the kiosk, leave it for the desktop,
# and come back. Used by the kiosk's Developer options (developer PIN):
# the agent starts this script DETACHED (setsid), because "restart" and
# "stop" also stop the agent itself.
#
#   bash kiosk_control.sh restart        stop everything, start it again, open the kiosk page
#   bash kiosk_control.sh desktop        close the kiosk page (programs keep running) + the Pi's on-screen keyboard
#   bash kiosk_control.sh desktop --stop same, and also stop the kiosk programs
#                                        (needed when a tool must use the sensors/cameras, e.g. bp_camTEST.py)
#   bash kiosk_control.sh terminal       close the kiosk page, open a terminal + the Pi's on-screen keyboard
#   bash kiosk_control.sh back           back to the kiosk: start the programs if they're stopped,
#                                        then open the kiosk page (the "MediKiosk" desktop icon runs this)
#   bash kiosk_control.sh stop           stop the kiosk programs and close their windows
#
# The Pi's own on-screen keyboard (squeekboard / wvkbd / matchbox) has Ctrl / Alt /
# Fn keys, so it must NEVER be up while the kiosk page is showing - someone could
# use it to get around the kiosk. Rule:
#   - outside the browser (desktop, terminal): it is opened, so you can type
#   - whenever the kiosk page opens (restart, back): it is closed first
# Inside the kiosk page, typing only ever uses the kiosk's built-in keyboard.
#
# Log: ~/medikiosk-logs/control.log
# =====================================================================
# Everything runs inside main() so bash reads the whole file first.

HW="$HOME/Desktop/MediKiosk_hardware"
URL="http://localhost:5173/kiosk"
LOG_DIR="$HOME/medikiosk-logs"
ICON_SRC="$HW/medikiosk-desktop-icon.desktop"

say() { echo "[control $(date '+%H:%M:%S')] $*"; }

# the five programs start_kiosk.sh opens (patterns for pgrep/pkill -f)
PROGRAMS=("agent/kiosk_agent.py" "edge/edge_service.py" "edge/sync_worker.py" "agent/save_readings.py" "mediKiosk_Frontend/node_modules/.bin/vite")

close_page() {
  if pkill -f -- "--kiosk.*localhost:5173/kiosk|localhost:5173/kiosk.*--kiosk"; then
    say "kiosk page closed"
  else
    say "kiosk page was not open"
  fi
}

stop_programs() {
  local p
  for p in "${PROGRAMS[@]}"; do pkill -f -- "$p" && say "stopping $p"; done
  # npm itself (parent of vite)
  pkill -f -- "npm run dev" 2>/dev/null
  for _ in $(seq 1 10); do                 # wait up to 5 s for a clean stop
    local alive=0
    for p in "${PROGRAMS[@]}"; do pgrep -f -- "$p" >/dev/null && alive=1; done
    [ "$alive" = 0 ] && break
    sleep 0.5
  done
  for p in "${PROGRAMS[@]}"; do pkill -9 -f -- "$p" && say "had to force-stop $p"; done
  # their windows now say "press Enter to close" - close them (only OUR windows: start_kiosk.sh's titles)
  pkill -f -- "echo '=== [0-9] [A-Za-z]+ ===';" 2>/dev/null
  pkill -f -- "echo '=== Old local backend ===';" 2>/dev/null
  say "kiosk programs stopped"
}

open_page() {
  local i BROWSER
  say "waiting for the website..."
  for i in $(seq 1 60); do                 # up to 2 minutes
    curl -s -o /dev/null "$URL" && break
    sleep 2
  done
  BROWSER=$(command -v chromium-browser || command -v chromium)
  if [ -z "$BROWSER" ]; then
    say "Chromium not found"
    return 1
  fi
  close_page >/dev/null
  keyboard_off
  say "opening $URL"
  # same flags as kiosk_autostart.sh
  "$BROWSER" --kiosk --noerrdialogs --disable-session-crashed-bubble --disable-infobars \
    --password-store=basic "$URL" >/dev/null 2>&1 &
}

keyboard_on() {   # outside the browser only (desktop / terminal)
  if command -v squeekboard >/dev/null; then
    pgrep -x squeekboard >/dev/null || (squeekboard >/dev/null 2>&1 &)
    sleep 1
    busctl call --user sm.puri.OSK0 /sm/puri/OSK0 sm.puri.OSK0 SetVisible b true >/dev/null 2>&1
    say "Pi on-screen keyboard: squeekboard on"
  elif command -v wvkbd-mobintl >/dev/null; then
    pgrep -x wvkbd-mobintl >/dev/null || (wvkbd-mobintl -L 300 >/dev/null 2>&1 &)
    say "Pi on-screen keyboard: wvkbd on"
  elif command -v matchbox-keyboard >/dev/null; then
    pgrep -x matchbox-keyboard >/dev/null || (matchbox-keyboard >/dev/null 2>&1 &)
    say "Pi on-screen keyboard: matchbox-keyboard on"
  else
    say "no on-screen keyboard installed for the desktop - sudo apt install squeekboard"
  fi
}

keyboard_off() {  # before the kiosk page shows: never leave the Pi's keyboard up over it
  busctl call --user sm.puri.OSK0 /sm/puri/OSK0 sm.puri.OSK0 SetVisible b false >/dev/null 2>&1
  pkill -x squeekboard 2>/dev/null && say "Pi on-screen keyboard closed (squeekboard)"
  pkill -x wvkbd-mobintl 2>/dev/null && say "Pi on-screen keyboard closed (wvkbd)"
  pkill -x matchbox-keyboard 2>/dev/null && say "Pi on-screen keyboard closed (matchbox)"
  return 0
}

install_icon() {   # the "MediKiosk" icon to come back: on the desktop + in the app menu
  [ -f "$ICON_SRC" ] || return
  mkdir -p "$HOME/.local/share/applications"
  cp "$ICON_SRC" "$HOME/.local/share/applications/medikiosk.desktop"
  if [ -d "$HOME/Desktop" ] && [ ! -f "$HOME/Desktop/MediKiosk.desktop" ]; then
    cp "$ICON_SRC" "$HOME/Desktop/MediKiosk.desktop"
    chmod +x "$HOME/Desktop/MediKiosk.desktop"
    say "MediKiosk icon put on the desktop (tap it to go back to the kiosk)"
  fi
}

main() {
  mkdir -p "$LOG_DIR"
  exec >>"$LOG_DIR/control.log" 2>&1
  echo
  say "==== $* ===="
  sleep 1        # let the agent send its answer to the screen first
  case "$1" in
    restart)
      close_page
      stop_programs
      if bash "$HW/start_kiosk.sh"; then
        open_page
      else
        say "start_kiosk.sh STOPPED (reason above) - showing this log on the screen"
        lxterminal --title="MediKiosk did not start" -e bash -c "tail -n 40 '$LOG_DIR/control.log'; read" &
      fi
      ;;
    desktop)
      close_page
      [ "$2" = "--stop" ] && stop_programs
      install_icon
      keyboard_on
      ;;
    terminal)
      close_page
      install_icon
      keyboard_on
      (cd "$HW" && lxterminal --title="MediKiosk terminal" --working-directory="$HW" >/dev/null 2>&1 &)
      say "terminal opened in $HW"
      ;;
    back)
      if ! pgrep -f -- "agent/kiosk_agent.py" >/dev/null; then
        bash "$HW/start_kiosk.sh" || { say "start_kiosk.sh STOPPED - see above"; exit 1; }
      fi
      open_page
      ;;
    stop)
      close_page
      stop_programs
      ;;
    *)
      say "unknown command '$1' - use restart | desktop [--stop] | terminal | back | stop"
      exit 1
      ;;
  esac
}

main "$@"; exit
