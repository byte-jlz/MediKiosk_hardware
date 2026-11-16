#!/usr/bin/env bash
# =====================================================================
# kiosk_autostart.sh - what the Pi does by itself after every boot:
#   1. update_kiosk.sh   get the newest kiosk-stable code from GitHub (if there is any)
#   2. start_kiosk.sh    open the 5 program windows (Agent, Edge, Sender, Backup, Website)
#   3. open the kiosk page full screen (Chromium kiosk mode) once the website answers
#
# Log of every boot:   ~/medikiosk-logs/autostart.log
#
# One-time setup on the Pi (see claude/how-to-run.md in the project):
#   sudo raspi-config  ->  System Options -> Boot / Auto Login -> Desktop Autologin
#   sudo systemctl enable mosquitto
#   mkdir -p ~/.config/autostart
#   cp ~/Desktop/MediKiosk_hardware/medikiosk-autostart.desktop ~/.config/autostart/
# Turn it off again:   rm ~/.config/autostart/medikiosk-autostart.desktop
#
# Leave the kiosk page: plug in a keyboard and press Alt+F4.
# =====================================================================
# Everything runs inside main() so bash reads the whole file first:
# update_kiosk.sh may replace this very file with a newer version while it runs.

main() {
  local HW="$HOME/Desktop/MediKiosk_hardware"
  local LOG="$HOME/medikiosk-logs/autostart.log"
  local URL="http://localhost:5173/kiosk"
  mkdir -p "$HOME/medikiosk-logs"
  exec >>"$LOG" 2>&1
  echo
  echo "==== boot $(date '+%Y-%m-%d %H:%M:%S') ===="

  sleep 15   # let the desktop, the USB devices (webcams, scale ESP32) and the Wi-Fi settle

  bash "$HW/update_kiosk.sh"

  if ! bash "$HW/start_kiosk.sh"; then
    echo "[autostart] start_kiosk.sh STOPPED (reason above) - showing this log on the screen"
    lxterminal --title="MediKiosk did not start" -e bash -c "tail -n 40 '$LOG'; echo; echo 'Fix it, then run: bash $HW/start_kiosk.sh --browser'; read" &
    exit 1
  fi

  echo "[autostart] waiting for the website..."
  local i
  for i in $(seq 1 60); do      # up to 2 minutes
    curl -s -o /dev/null "$URL" && break
    sleep 2
  done
  curl -s -o /dev/null "$URL" || echo "[autostart] the website still doesn't answer - opening the page anyway"

  local BROWSER
  BROWSER=$(command -v chromium-browser || command -v chromium)
  if [ -z "$BROWSER" ]; then
    echo "[autostart] Chromium not found"
    exit 1
  fi
  echo "[autostart] opening $URL"
  # --disable-session-crashed-bubble: no "Restore pages?" popup after the power was cut
  "$BROWSER" --kiosk --noerrdialogs --disable-session-crashed-bubble --disable-infobars "$URL" >/dev/null 2>&1 &
}

main "$@"; exit
