#!/usr/bin/env python3
"""
bp_auto.py - fully automatic BP measurement:
  1. presses the BP monitor's START button (Raspberry Pi GPIO or ESP32 over USB)
  2. watches the LCD with the webcam (uses your tuned bp_cam_settings.json)
  3. waits until the final SYS/DIA/PUL result is on screen and stays steady
  4. saves a picture of the result + appends it to bp_results.csv

Timing (from our test runs: fastest 37.9 s, slowest 69.2 s):
  - the screen is ignored for the first MIN_WAIT seconds (cuff pressure numbers)
  - after that, a result is accepted once the same valid SYS/DIA/PUL is seen for
    HOLD_SEC seconds
  - if no result by TIMEOUT seconds -> reported as an error (e.g. "E" on screen)

Usage:
  python bp_auto.py --trigger esp32 --port COM5        # Windows laptop + ESP32
  python bp_auto.py --trigger esp32 --port /dev/ttyUSB0 # Pi + ESP32
  python bp_auto.py --trigger pi                        # Pi only: START on GPIO17 (pin 11), lights relay on GPIO26
  python bp_auto.py --trigger manual                    # you press the button yourself
  add --loop to measure again after each result, --no-window to run without a preview
  python bp_auto.py --check-screen                      # test ON/OFF detection only

The START button is an ON/OFF toggle, so before pressing, the script checks the
screen: if the monitor is already ON (e.g. still showing the last result), it
presses once to turn it OFF, waits for the blank screen, then presses START.

Error screens (e.g. "Er" in the SYS box and "P" in the DIA box) are watched for
during the WHOLE measurement. When one is confirmed, the script stops, saves a
picture, logs it to bp_errors.csv, tells the user what to check, and waits for
Enter to try again.
"""

import argparse
import csv
import importlib
import json
import os
import subprocess
import sys
import time
from collections import Counter, deque

import cv2

# reuse the tuned camera reader (works with either file name)
try:
    import pi_pins
except ImportError:
    from hardware import pi_pins

try:
    import bp_camTEST as cam
except ImportError:
    cam = importlib.import_module("test_bp_camera")

# ---------------- Timing config ----------------
MIN_WAIT = 30       # s: don't accept any result before this (fastest run was ~38 s)
TIMEOUT = 100       # s: give up after this (slowest run was ~69 s)
HOLD_SEC = 3.0      # s: result must stay the same this long to be accepted
AGREE = 0.8         # share of frames in the hold window that must agree
MIN_CHANGED = 5     # frames that must look "not a result" after the press
                    # (proves the screen left the old reading and is measuring)

# ---------------- Power-state check (START button is an on/off toggle) ----------------
STATE_CHECK_SEC = 1.5   # s: look at the screen this long to decide ON or OFF
OFF_TIMEOUT = 6         # s: after an "off" press, screen must go blank within this
ON_TIMEOUT = 5          # s: after a "start" press, digits must appear within this
SETTLE_AFTER_OFF = 2.0  # s: pause between turning off and starting again
CONFIRM_FRAMES = 3      # frames in a row needed to confirm ON/OFF

# ---------------- Error screens ----------------
ERROR_GRACE_SEC = 10    # s: ignore "errors" right after START - the monitor shows
                        # 888 / 0 / blank segments while the pump spins up, and the
                        # camera can misread that as "Er P". Raise if false errors remain.
ERROR_HOLD_SEC = 2.5    # s: error must stay on screen this long to count
ERROR_AGREE = 0.7       # share of frames in that time that must show the error
# the error code table lives in bp_camTEST.py (ERROR_CODES) - add new codes there

HERE = os.path.dirname(os.path.abspath(__file__))
RESULT_DIR = os.path.join(HERE, "bp_results")
RESULT_CSV = os.path.join(HERE, "bp_results.csv")
ERROR_CSV = os.path.join(HERE, "bp_errors.csv")

# ---------------- Pi pins ----------------
# BP monitor START button: physical pin 11 = GPIO17 (BCM numbering, used by pinctrl)
BP_START_PIN = 17
if getattr(pi_pins, "BP_BUTTON", BP_START_PIN) != BP_START_PIN:
    print(f"[warn] pi_pins.BP_BUTTON is GPIO{pi_pins.BP_BUTTON}, but bp_auto.py uses "
          f"GPIO{BP_START_PIN} (pin 11) for START - update pi_pins.py to match")


# ---------------- Button triggers ----------------
class PiTrigger:
    """Does on the Pi what the ESP32 sketch used to do:
    presses START (GPIO17 via pinctrl) and switches the camera-light relay (GPIO26)."""

    def __init__(self, pin=BP_START_PIN, press_s=0.9, led_pin=26):
        self.pin, self.press_s, self.led_pin = pin, press_s, led_pin
        self.release()
        if self.led_pin is not None:
            pi_pins.lights(False, self.led_pin)   # start with lights off

    def release(self):
        pi_pins.release_bp_button(self.pin)

    def press(self):
        pi_pins.press_bp_button(int(self.press_s * 1000), self.pin)  # = ESP32 "PRESS"

    def led(self, on):
        if self.led_pin is not None:               # = ESP32 "LED_ON" / "LED_OFF"
            pi_pins.lights(on, self.led_pin)

    def close(self):
        self.release()
        self.led(False)


class Esp32Trigger:
    """Sends PRESS to the ESP32 (bp_trigger_esp32.ino) over USB serial."""

    def __init__(self, port, press_ms=900, baud=115200):
        try:
            import serial
        except ImportError:
            raise SystemExit("[error] pyserial missing: pip install pyserial")
        self.press_ms = press_ms
        self.ser = serial.Serial(port, baud, timeout=3)
        time.sleep(2.0)                    # ESP32 resets when the port opens
        self.ser.reset_input_buffer()
        self.ser.write(b"PING\n")
        if "PONG" not in self.ser.readline().decode(errors="ignore"):
            raise SystemExit(f"[error] no reply from ESP32 on {port} - check port/sketch")
        print(f"[info] ESP32 ready on {port}")

    def press(self):
        self.ser.reset_input_buffer()
        self.ser.write(f"PRESS {self.press_ms}\n".encode())
        reply = self.ser.readline().decode(errors="ignore").strip()
        if reply != "OK":
            raise RuntimeError(f"ESP32 did not confirm press (got {reply!r})")

    def led(self, on):
        """Turn the camera lights on/off (sketch must answer LED_ON / LED_OFF with OK)."""
        cmd = "LED_ON" if on else "LED_OFF"
        self.ser.reset_input_buffer()
        self.ser.write(f"{cmd}\n".encode())
        reply = self.ser.readline().decode(errors="ignore").strip()
        if reply != "OK":
            raise RuntimeError(f"ESP32 did not confirm {cmd} (got {reply!r})")

    def close(self):
        self.ser.close()


class ManualTrigger:
    slow = True                            # a human needs more time to react

    def press(self):
        print(">>> Press the START button on the BP monitor now")

    def led(self, on):
        pass

    def close(self):
        pass


# ---------------- Reading helpers ----------------
def read_all(frame):
    """Decode SYS/DIA/PUL text from one frame using the saved boxes."""
    H, W = frame.shape[:2]
    out = {}
    for field in cam.FIELDS:
        r = cam.settings["rois"].get(field)
        if not r:
            raise SystemExit(f"[error] no {field} box saved - run bp_camTEST.py and press w")
        x, y, w, h = r
        x, y = max(0, min(x, W - 2)), max(0, min(y, H - 2))
        w, h = max(2, min(w, W - x)), max(2, min(h, H - y))
        _, binary = cam.process(frame[y:y + h, x:x + w], cam.settings)
        text, _, _ = cam.read_number(binary, cam.settings["seg_on"] / 100.0)
        out[field] = text
    return out


def valid_result(vals):
    """Return (sys, dia, pul) if the screen shows a plausible final result."""
    nums = []
    for field in cam.FIELDS:
        t = vals.get(field)
        if not t or not t.isdigit():
            return None
        lo, hi = cam.VALID_RANGE[field]
        n = int(t)
        if not lo <= n <= hi:
            return None
        nums.append(n)
    sys_, dia, pul = nums
    return (sys_, dia, pul) if sys_ > dia else None


error_code = cam.error_code               # shared with bp_camTEST.py


def draw_status(frame, vals, lines, color):
    disp = frame.copy()
    for field in cam.FIELDS:
        x, y, w, h = cam.settings["rois"][field]
        cv2.rectangle(disp, (x, y), (x + w, y + h), cam.COLORS[field], 3)
        cv2.putText(disp, f"{field} {vals.get(field) or '--'}", (x, max(30, y - 10)),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.1, cam.COLORS[field], 3)
    for i, line in enumerate(lines):
        cv2.putText(disp, line, (15, 45 + i * 45), cv2.FONT_HERSHEY_SIMPLEX, 1.3, color, 3)
    return disp


def show(disp, enabled):
    if not enabled:
        return
    scale = min(1.0, 960 / disp.shape[1])
    cv2.imshow("BP Auto", cv2.resize(disp, None, fx=scale, fy=scale))
    cv2.waitKey(1)


# ---------------- Power state ----------------
def screen_on(vals):
    """Screen is ON if any of the three boxes shows digits (old result, 888, pressure...).
    A switched-off LCD is blank, so all three boxes read nothing."""
    return any(vals.get(f) for f in cam.FIELDS)


def check_screen(cap, window, seconds=STATE_CHECK_SEC):
    """Watch the screen for a moment and vote: True = ON, False = OFF."""
    on = off = 0
    t_end = time.time() + seconds
    while time.time() < t_end or on + off < 3:
        ok, frame = cap.read()
        if not ok:
            time.sleep(0.05)
            continue
        vals = read_all(frame)
        if screen_on(vals):
            on += 1
        else:
            off += 1
        show(draw_status(frame, vals, ["CHECKING SCREEN..."], (255, 200, 0)), window)
    return on > off


def wait_for_state(cap, want_on, timeout, window, label):
    """Wait until the screen is ON (want_on=True) or OFF for CONFIRM_FRAMES frames in a row."""
    t0, streak = time.time(), 0
    while time.time() - t0 < timeout:
        ok, frame = cap.read()
        if not ok:
            time.sleep(0.05)
            continue
        vals = read_all(frame)
        streak = streak + 1 if screen_on(vals) == want_on else 0
        show(draw_status(frame, vals, [label], (255, 200, 0)), window)
        if streak >= CONFIRM_FRAMES:
            return True
    return False


def start_bp(cap, trigger, window):
    """Make sure the monitor ends up MEASURING, whatever state it was in.
    Returns the start time, or None if the monitor didn't respond."""
    slow = getattr(trigger, "slow", False)
    off_timeout = OFF_TIMEOUT * (3 if slow else 1)
    on_timeout = ON_TIMEOUT * (3 if slow else 1)

    for _ in range(5):                     # flush old frames from the camera buffer
        cap.read()

    if check_screen(cap, window):
        print("[info] BP monitor is already ON (old result on screen) -> turning it OFF first")
        trigger.press()
        if not wait_for_state(cap, False, off_timeout, window, "TURNING OFF..."):
            print("[error] monitor did not turn off - check the button wiring")
            return None
        print("[info] monitor is OFF")
        t_end = time.time() + SETTLE_AFTER_OFF
        while time.time() < t_end:         # keep the camera buffer fresh while waiting
            cap.read()
    else:
        print("[info] BP monitor is OFF")

    print("[info] pressing START")
    trigger.press()
    t0 = time.time()
    if not wait_for_state(cap, True, on_timeout, window, "STARTING..."):
        print("[error] monitor did not turn on - check wiring / batteries")
        return None
    return t0


# ---------------- One measurement ----------------
def measure(cap, trigger, window=True):
    t0 = start_bp(cap, trigger, window)
    if t0 is None:
        return None
    print(f"[info] BP started at {time.strftime('%H:%M:%S')} - waiting for result...")

    changed = 0                            # frames that didn't look like a result
    window_hits = deque()                  # (time, result-or-None) in the hold window
    err_hits = deque()                     # (time, error-code-or-None) in the error window
    last_print = 0

    while True:
        ok, frame = cap.read()
        if not ok:
            time.sleep(0.1)
            continue
        now = time.time()
        el = now - t0
        vals = read_all(frame)
        res = valid_result(vals)

        if res is None:
            changed += 1

        # error check starts after the pump-up grace period, then runs to the end
        if el < ERROR_GRACE_SEC:
            if error_code(vals) and now - last_print > 1:
                print(f"   {el:5.1f}s  error-like screen ignored (pump starting): {vals}")
                last_print = now
            show(draw_status(frame, vals, [f"PUMPING {el:5.1f}s"], (0, 165, 255)), window)
            continue
        err_hits.append((now, error_code(vals)))
        while err_hits and now - err_hits[0][0] > ERROR_HOLD_SEC:
            err_hits.popleft()
        codes = [c for _, c in err_hits if c]
        if codes and now - err_hits[0][0] >= ERROR_HOLD_SEC * 0.9 \
                and len(codes) / len(err_hits) >= ERROR_AGREE:
            known = [c for c in codes if c != "?"]
            code = Counter(known).most_common(1)[0][0] if known else "?"
            return report_error(frame, vals, code, el, window)

        status = f"MEASURING {el:5.1f}s"
        if el >= MIN_WAIT and changed >= MIN_CHANGED:
            window_hits.append((now, res))
            while window_hits and now - window_hits[0][0] > HOLD_SEC:
                window_hits.popleft()
            hits = [r for _, r in window_hits if r]
            if hits and now - window_hits[0][0] >= HOLD_SEC * 0.9:
                best, count = Counter(hits).most_common(1)[0]
                if count / len(window_hits) >= AGREE:
                    return finish(frame, vals, best, el, window)
            status = f"CHECKING {el:5.1f}s"

        if el > TIMEOUT:
            print(f"[error] no stable result after {TIMEOUT}s (last screen: {vals})")
            save_picture(frame, vals, None, el, error=True)
            show(draw_status(frame, vals, ["ERROR - no result", "check cuff / retry"],
                             (0, 0, 255)), window)
            return None

        if now - last_print > 5:
            print(f"   {el:5.1f}s  screen: SYS={vals['SYS']} DIA={vals['DIA']} PUL={vals['PUL']}")
            last_print = now
        show(draw_status(frame, vals, [status], (0, 165, 255)), window)


def report_error(frame, vals, code, el, window):
    label, message = cam.error_text(code)
    ts, path = save_picture(frame, vals, None, el, error=True, tag=f"ERROR_Er{code.strip('?')}")
    new_file = not os.path.exists(ERROR_CSV)
    with open(ERROR_CSV, "a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["timestamp", "code", "message", "after_s", "picture"])
        w.writerow([ts, label, message, f"{el:.1f}", os.path.basename(path)])
    print(f"[ERROR] {json.dumps({'error': label, 'message': message, 'after_s': round(el, 1)})}")
    show(draw_status(frame, vals, [f"ERROR {label}", "check the cuff"], (0, 0, 255)), window)
    return {"error": label, "message": message}


def save_picture(frame, vals, res, el, error=False, tag=None):
    os.makedirs(RESULT_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    tag = tag or ("ERROR" if error else f"{res[0]}-{res[1]}-{res[2]}")
    lines = ["ERROR" if error else f"{res[0]}/{res[1]}  PUL {res[2]}", f"{el:.1f}s  {ts}"]
    path = os.path.join(RESULT_DIR, f"{ts}_{tag}.jpg")
    cv2.imwrite(path, draw_status(frame, vals, lines, (0, 0, 255) if error else (0, 200, 0)))
    return ts, path


def finish(frame, vals, res, el, window):
    ts, path = save_picture(frame, vals, res, el)
    new_file = not os.path.exists(RESULT_CSV)
    with open(RESULT_CSV, "a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["timestamp", "sys", "dia", "pul", "duration_s", "picture"])
        w.writerow([ts, *res, f"{el:.1f}", os.path.basename(path)])
    result = {"sys": res[0], "dia": res[1], "pul": res[2], "duration_s": round(el, 1)}
    print(f"[RESULT] {json.dumps(result)}")
    print(f"[info] picture saved: {path}")
    show(draw_status(frame, vals, [f"DONE {res[0]}/{res[1]}  PUL {res[2]}"], (0, 200, 0)), window)
    return result


# ---------------- Main ----------------
def main():
    global MIN_WAIT, TIMEOUT, HOLD_SEC, ERROR_GRACE_SEC, ERROR_HOLD_SEC
    ap = argparse.ArgumentParser()
    ap.add_argument("--trigger", choices=["pi", "esp32", "manual"],
                    default="pi" if sys.platform.startswith("linux") else "esp32")
    ap.add_argument("--port", help="ESP32 serial port, e.g. COM5 or /dev/ttyUSB0")
    ap.add_argument("--pin", type=int, default=BP_START_PIN,
                    help="Pi GPIO (BCM) for the START button, --trigger pi (default 17 = pin 11)")
    ap.add_argument("--led-pin", type=int, default=pi_pins.LIGHT_RELAY,
                    help="Pi GPIO driving the light relay (-1 = no lights)")
    ap.add_argument("--press-ms", type=int, default=900)
    ap.add_argument("--cam", type=int, default=0)
    ap.add_argument("--backend", choices=list(cam.BACKENDS), default=None)
    ap.add_argument("--min-wait", type=float, default=MIN_WAIT)
    ap.add_argument("--timeout", type=float, default=TIMEOUT)
    ap.add_argument("--hold", type=float, default=HOLD_SEC)
    ap.add_argument("--error-grace", type=float, default=ERROR_GRACE_SEC,
                    help="seconds after START during which error screens are ignored")
    ap.add_argument("--error-hold", type=float, default=ERROR_HOLD_SEC,
                    help="seconds an error must stay on screen to count")
    ap.add_argument("--loop", action="store_true", help="measure again after each result")
    ap.add_argument("--no-window", action="store_true", help="no preview window")
    ap.add_argument("--check-screen", action="store_true",
                    help="only print whether the camera sees the monitor as ON or OFF")
    args = ap.parse_args()
    MIN_WAIT, TIMEOUT, HOLD_SEC = args.min_wait, args.timeout, args.hold
    ERROR_GRACE_SEC, ERROR_HOLD_SEC = args.error_grace, args.error_hold
    window = not args.no_window

    if args.check_screen:                  # quick test, no button presses
        cap = cam.open_camera(args.cam, cam.FRAME_W, cam.FRAME_H, args.backend)
        cam.apply_exposure(cap, cam.settings["exposure"])
        print("Turn the BP monitor on/off and watch the result. Ctrl+C to stop.")
        try:
            while True:
                print("screen is", "ON" if check_screen(cap, window) else "OFF")
        except KeyboardInterrupt:
            pass
        cap.release()
        cv2.destroyAllWindows()
        return

    if args.trigger == "esp32":
        if not args.port:
            raise SystemExit("[error] give the ESP32 port, e.g. --port COM5")
        trigger = Esp32Trigger(args.port, args.press_ms)
    elif args.trigger == "pi":
        trigger = PiTrigger(args.pin, args.press_ms / 1000,
                             None if args.led_pin < 0 else args.led_pin)
    else:
        trigger = ManualTrigger()

    def run_once():
        """Lights on first, then the camera, so it never starts in the dark.
        The camera is opened per measurement and released afterwards."""
        trigger.led(True)                  # lights on for the whole measurement
        cap = None
        try:
            time.sleep(cam.LIGHT_WARMUP_SEC)
            cap = cam.open_camera(args.cam, cam.FRAME_W, cam.FRAME_H, args.backend)
            cam.apply_exposure(cap, cam.settings["exposure"])
            cam.warm_up(cap)               # let auto-exposure adjust to the lit screen
            return measure(cap, trigger, window)  # on/off check + full measurement
        finally:
            if cap is not None:
                cap.release()
            try:
                trigger.led(False)         # always off, even on error or Ctrl+C
            except (RuntimeError, OSError) as e:
                print(f"[warn] could not turn the lights off: {e}")

    try:
        while True:
            input("\nPut the cuff on, then press Enter to start the BP measurement...")
            while True:
                outcome = run_once()
                if outcome and "error" not in outcome:
                    break                  # good result
                print("\n" + "=" * 50)
                if outcome:                # error shown on the BP monitor
                    print(f'  "An error occurred" ({outcome["error"]})')
                    print(f"  {outcome['message']}")
                else:                      # no result / monitor did not respond
                    print('  "An error occurred" - no result from the BP monitor.')
                    print("  Please check the cuff and the BP monitor.")
                print("=" * 50)
                ans = input("Press ENTER to try again (or type q then Enter to cancel)... ")
                if ans.strip().lower() == "q":
                    print("[info] measurement cancelled")
                    break
            if not args.loop:
                input("Press Enter to exit...")
                break
    except KeyboardInterrupt:
        print("\n[info] stopped")
    finally:
        trigger.close()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()