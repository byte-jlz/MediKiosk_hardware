#!/usr/bin/env python3
"""
measurements.py - every MediKiosk measurement as a plain function.

The measuring code lives here ONCE, so the terminal test tool
(sensor_test/sensor_test.py) and the MQTT kiosk agent (next step) run
exactly the same code.

    measure_hr_spo2()   heart rate + SpO2      (MAX30102)
    measure_rr()        respiratory rate       (same MAX30102 signal)
    measure_finger()    RR + SpO2 + HR from ONE finger reading (RR_SECONDS, kiosk flow)
    measure_height()    distance to head       (VL53L0X ToF)
    measure_weight()    weight                 (ESP32 + HX711, over USB)
    measure_temp()      temperature            (MLX90614)
    measure_bp()        SYS / DIA / PUL        (BP monitor + webcam + START press)

Rules for this file
-------------------
* No input() and no print() - it never talks to the person directly.
* Results are plain numbers in JSON-friendly dicts (ready for MQTT), not text.
* The caller decides how to talk to the person, through two callbacks
  (both optional):

    confirm(step, message)
        Called when the person must do something first, e.g.
        confirm("cuff_on", "Put the cuff on (arm at heart level)").
        Return when they are ready. sensor_test.py waits for Enter;
        the kiosk tablet will show a button.
        A confirm() that also takes `until=` (the kiosk agent's does) can
        finish BY ITSELF: it calls until() every ~0.1 s and returns as soon
        as it is True (e.g. "the platform is empty" for the auto-zero), the
        button stays as a backup. Callers without it just wait as before.

    progress(event)
        Called while measuring with a dict like
            {"vital": "hr", "phase": "measuring",
             "message": "valid HR samples:  4", "seconds_left": 12.3,
             "live": {"valid_samples": 4}}
        phases: "info", "warning", "measuring" (countdown tick),
                "countdown_end" (countdown finished)
        sensor_test.py prints it; the agent will publish it on MQTT.

  Raising an exception (e.g. Cancelled) inside either callback stops the
  measurement cleanly - sensors are still closed and the I2C bus released.

* "No reading" outcomes (no finger, nothing under the sensor, ...) are
  RETURNED with an "error" text, so the UI can say "try again".
  Hardware problems (sensor missing, wiring, library missing) RAISE
  MeasureError (a RuntimeError).

All the tuning settings are below - this is the one place they live now.
"""

import inspect
import json
import os
import sys
import time

# This file sits in hardware/ next to the drivers (max30102.py, hrcalc.py,
# bp_auto.py, ...). Make sure they can be imported from anywhere.
HW_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(HW_DIR)
if HW_DIR not in sys.path:
    sys.path.insert(0, HW_DIR)

ON_PI = sys.platform.startswith("linux")

# =====================================================================
# Settings (tune here)
# =====================================================================
I2C_BUS = 1

# Heart rate (MAX30102)
HR_SECONDS = 20          # how long to read
HR_RANGE = (40, 200)     # plausible bpm, anything else is dropped
SPO2_RANGE = (70, 100)   # plausible % (checked on the sensor's own value, before the calibration)
# SpO2 CALIBRATION: added to the sensor's SpO2. The formula in hrcalc.py is Maxim's, made for
# their own board - this MAX30102 board reads lower. Work it out against a medical pulse
# oximeter: take several readings at the same time on different people, then
#     SPO2_OFFSET = average of (oximeter - kiosk's spo2_raw_pct)
# 2026-11-11: one comparison, oximeter 97 % vs sensor 86 %  ->  +11.  CHECK WITH MORE READINGS.
SPO2_OFFSET = 11.0
SPO2_MAX = 100.0         # never show more than 100 %
FINGER_IR_MIN = 50000    # raw IR below this = no finger on the sensor
# Live finger detection (Nov 2026, kiosk finger step) - all to be checked in the field
FINGER_STEADY_SEC = 2.0      # finger on + steady this long -> the reading starts by itself
FINGER_STEADY_FRAC = 0.15    # "steady" = the IR level moves less than 15 % meanwhile
FINGER_MOVE_FRAC = 0.20      # IR jumps more than 20 % between half-second blocks = hand moving (hint only)
FINGER_OFF_MAX_SEC = 2.0     # ONE continuous lift longer than this -> that reading restarts right away
FINGER_MAX_RESTARTS = 2      # at most this many restarts, then "no steady reading"
FINGER_BLOCK = 12            # samples per live check while recording (~0.5 s at 25/s)
SENSOR_DEAD_SEC = 5.0        # IR stuck at exactly 0 or at its maximum this long = sensor problem
IR_SATURATED = 262000        # (the 18-bit maximum is 262143)
MAX30102_ADDR = 0x57
MAX30102_PART_ID = 0x15

# Respiratory rate (from the same MAX30102 signal)
RR_SECONDS = 30          # 2026-11-10: 30 s (was 60) for a shorter kiosk step. Replay of the 20
                         # hand-counted runs: 60 s = 15/20 readings, 2 off by >2; 30 s with the
                         # scaled peak-count rule below = 15/20 readings, 4 off by >2.
                         # (30 s strict = 6/20, 40 s = 14/20 with 0 off.) Set back to 60 for accuracy.
RR_BAND_HZ = (8 / 60, 0.5)  # breathing speeds the filter keeps (= 8-30 breaths/min)
RR_RANGE = (8, 30)       # plausible breaths/min
RR_FFT_MIN_TRUST = 9.5   # FFT answers below this sit on the ~6-8/min blood-vessel wave: not trusted
RR_AGREE = 3             # peak count and FFT must agree within this many breaths/min
RR_EDGE_SEC = 3          # ignore the first/last 3 s of the filtered wave
RR_MIN_STRENGTH = 6.0    # breathing peak must stand this many x above the rest of the band
RR_MIN_STRENGTH_AGREE = 4.0   # ...or only this much if peak count and FFT agree within
RR_TIGHT_AGREE = 1.0          # this many breaths/min (two independent methods agreeing
                              # is strong evidence on its own - 1st real test: 5.1, agree 0.2)
RR_MAX_UNEVEN = 0.25     # breath-to-breath gap variation (std/mean); noise is ~0.3+
RR_MAX_JUMP = 0.05       # a 1-second jump bigger than 5% of the signal = finger moved
# (these 3 were picked from simulated signals - check the "Checks" line on real
#  users and adjust if good readings get rejected)
RR_LOG_DIR = os.path.join(REPO_DIR, "sensor_test", "rr_logs")   # CSV + plot of every RR test

# Height (VL53L0X)
HEIGHT_SECONDS = 5
SENSOR_MOUNT_MM = 1994   # sensor height above the surface the person STANDS ON, in mm.
                         # None = only report the distance to the head.
TOF_SCALE = 1.0          # corrects a sensor that reads long/short by a %.
#   height_mm = SENSOR_MOUNT_MM - TOF_SCALE * raw_distance_mm
# Calibrated with: python3 sensor_test/sensor_test.py --calibrate-height
# (SENSOR_MOUNT_MM then also absorbs the sensor's fixed offset, so after a
#  calibration it may differ a little from the tape-measured mount height.)
# NOTE: values saved in hardware/calibration.json OVERRIDE the numbers here.
TOF_RANGE_MM = (30, 2500)
TOF_TIMING_BUDGET_US = 200000   # 200 ms = steadier readings
# Max seconds one VL53L0X reading may take. The Adafruit library's default is 0 =
# WAIT FOREVER: a stuck sensor then freezes the program (and the agent stays "busy").
TOF_IO_TIMEOUT_S = 1.0

# Weight (ESP32 + HX711 - see the WEIGHT section below)
WEIGHT_SECONDS = 5
STEP_ON_SETTLE_SEC = 0.5   # pause after "I'm on the scale" before weighing starts

# Auto-zero (Nov 2026): the patient never has to step off just to tare.
# While the kiosk is idle the agent keeps an eye on the scale: an EMPTY,
# STEADY platform is re-zeroed by itself. When the BMI reading starts and that
# zero is still fresh, weighing starts straight away. Only when the zero can't
# be trusted is the patient asked to step off (and even then it tares by itself
# once the platform is empty - the button is just a backup).
AUTO_ZERO = True
EMPTY_MAX_KG = 1.5           # idle: within this of the last zero = the platform is empty
ZERO_OK_KG = 0.2             # empty AND within this of zero = the zero is still good, no tare needed
EMPTY_STEADY_SEC = 3.0       # idle: empty + steady this long before it counts
STEADY_TOL_KG = 0.15         # "steady" = the readings move less than this (max - min)
ZERO_FRESH_SEC = 15 * 60     # a zero checked within this long is trusted -> no "step off"
ZERO_VERIFY_SEC = 1.0        # after every tare the reading must stay ~0 this long...
ZERO_VERIFY_TOL_KG = 0.3     # ...within this, or someone stepped on while it tared
FALLBACK_EMPTY_MAX_KG = 8.0  # after "please step off": below this + steady = empty enough to tare
FALLBACK_STEADY_SEC = 2.0
PERSON_MIN_KG = 10.0         # above this + steady = someone is standing on it
PERSON_STEADY_SEC = 1.5      # (also the settle time after tapping "Start measuring")
PERSON_STEADY_TOL_KG = 0.3
STAND_UNDER_AUTO_SEC = 2.0   # BMI: height starts by itself this long after the weight
# Stable name of the scale ESP32 on the Pi (CH340 USB chip). If you ever add a
# 2nd CH340 board, switch to its /dev/serial/by-path/... name instead.
SCALE_PORT = "/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0"
SCALE_BAUD = 115200
SCALE_BOOT_SEC = 8        # opening the port resets the ESP32: boot + 2 s settle + tare
CAL_EXPECTED_PER_G = 19.17   # factor from the 5 kg calibration on 2026-10-27
CAL_SETTLE_SEC = 3        # wait this long after the known weight is placed

# Temperature (MLX90614)
MLX90614_ADDR = 0x5A
TEMP_SECONDS = 5
TEMP_OFFSET_C = 2.9      # wrist skin -> body temp: MLX at the wrist 33.4 vs ARMPIT thermometer 36.3 (2026-10-30, 1 reading)
# Wrist detection + safety (Nov 2026) - all to be tuned in the field with the agent log
TEMP_DETECT_DIFF_C = 3.0      # wrist = object at least this much warmer than the sensor chip (the room)...
TEMP_HOT_ROOM_OBJ_C = 31.5    # ...or, on hot days: object at least this warm (raw, before the offset)
TEMP_HOT_ROOM_DIFF_C = 1.5    #    AND at least this much warmer than the room
TEMP_DETECT_STEADY_SEC = 1.0  # wrist there this long -> the 5 s reading starts by itself
TEMP_WOBBLE_WARN_C = 0.5      # readings swinging more than this = "Hold your wrist still" (warning only)
TEMP_WOBBLE_MAX_C = 1.0       # the steady part still swinging more than this = restart automatically
TEMP_MAX_RESTARTS = 2         # at most this many automatic restarts, then "ask the staff"
TEMP_MIN_BODY_C = 35.0        # result below this = wrist too far / cold -> try again (nothing saved)
TEMP_MAX_BODY_C = 43.0        # result above this = something hot in front of the sensor (fevers are accepted)
TEMP_CHIP_RANGE_C = (0.0, 60.0)  # the sensor's own chip temperature outside this = sensor problem
# Calibrated with: python3 sensor_test/sensor_test.py --calibrate-temp
# NOTE: a value saved in hardware/calibration.json OVERRIDES the number here.

# Blood pressure
# BP cam = hub port 1.1 = TOP BLUE port (QR cam = 1.2 = bottom blue, same camera
# model). Changed 2026-11-09 (was port 1.3). The by-path name follows the USB
# PORT, so it stays right after a reboot - keep each camera in its own port.
BP_CAMERA = "/dev/v4l/by-path/platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.1:1.0-video-index0"
# QR login cam = the same webcam model, so it is told apart ONLY by its USB port:
# hub port 1.2 (bottom blue). The agent opens it for the kiosk's "Scan your QR code" screen.
QR_CAMERA = "/dev/v4l/by-path/platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.2:1.0-video-index0"
BP_TRIGGER = "pi"        # auto | esp32 | pi | manual  (Pi presses START on GPIO27, pin 13)
BP_PI_PIN = 27          # was 17 (pin 11) - damaged 2026-10-31
BP_PRESS_MS = 900

ESP32_USB_IDS = {0x10C4, 0x1A86, 0x0403, 0x303A}   # CP210x, CH340, FTDI, Espressif USB

TRIM = 0.1               # trimmed average: drop the lowest/highest 10% of samples


# =====================================================================
# Calibration file - written by sensor_test.py's calibration options,
# loaded here on every start, so sensor_test.py AND the kiosk agent use it.
# (The load cell is different: its factor lives in the ESP32's flash.)
# Commit it to git like any other file so the Pi gets it with git pull.
# Delete it to go back to the numbers above. Restart the kiosk agent after calibrating.
# =====================================================================
CALIBRATION_FILE = os.path.join(HW_DIR, "calibration.json")
CALIBRATED_KEYS = ("SENSOR_MOUNT_MM", "TOF_SCALE", "TEMP_OFFSET_C")
CALIBRATION_DEFAULTS = {k: globals()[k] for k in CALIBRATED_KEYS}   # the numbers above


def _read_calibration_file():
    try:
        with open(CALIBRATION_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        print(f"[measurements] ignoring broken {CALIBRATION_FILE}: {e}", file=sys.stderr)
        return {}


def load_calibration():
    """Apply hardware/calibration.json on top of the defaults. Returns what was applied."""
    applied = {}
    for key, value in _read_calibration_file().items():
        if key in CALIBRATED_KEYS and isinstance(value, (int, float)):
            globals()[key] = value
            applied[key] = value
    return applied


def save_calibration(values, note=""):
    """Merge {KEY: number} into hardware/calibration.json and use it right away."""
    bad = [k for k in values if k not in CALIBRATED_KEYS]
    if bad:
        raise ValueError(f"not a calibration setting: {bad}")
    data = _read_calibration_file()
    log = data.get("_history", [])
    stamp = time.strftime("%Y-%m-%d %H:%M")
    for key, value in values.items():
        log.append({"when": stamp, "key": key, "old": globals()[key], "new": value, "note": note})
        data[key] = value
        globals()[key] = value
    data["_history"] = log[-30:]                   # keep the last 30 changes
    tmp = CALIBRATION_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, CALIBRATION_FILE)              # never leaves a half-written file


def calibration_status():
    """{KEY: (current value, 'calibration.json' or 'default')} - for printouts."""
    saved = _read_calibration_file()
    return {k: (globals()[k], "calibration.json" if k in saved else "default")
            for k in CALIBRATED_KEYS}


CALIBRATION_APPLIED = load_calibration()


# =====================================================================
# Errors + the two callbacks
# =====================================================================
class MeasureError(RuntimeError):
    """A hardware / setup problem: sensor missing, wiring, library not installed."""


class Cancelled(Exception):
    """Raise this from confirm() or progress() to stop a measurement."""


def _no_confirm(step, message, **_kw):
    pass                 # nobody to ask -> go straight on


def _confirm_auto(confirm, step, message, until):
    """confirm() that may also finish by itself once until() is True.

    The kiosk agent's confirm takes `until=`; older callers (sensor_test.py)
    don't, so they simply get the normal confirm (wait for Enter / a button).
    """
    try:
        params = inspect.signature(confirm).parameters.values()
        takes_until = any(p.name == "until" or p.kind == p.VAR_KEYWORD for p in params)
    except (TypeError, ValueError):
        takes_until = False
    if takes_until:
        confirm(step, message, until=until)
    else:
        confirm(step, message)


class SteadyWatch:
    """Remembers the last few seconds of readings and says when they are steady."""

    KEEP_SEC = 10.0

    def __init__(self):
        self.vals = []                    # (time, value)

    def add(self, value, t=None):
        t = time.time() if t is None else t
        self.vals.append((t, value))
        cut = t - self.KEEP_SEC
        while self.vals and self.vals[0][0] < cut:
            self.vals.pop(0)

    def clear(self):
        self.vals = []

    def steady(self, seconds, tol, below=None, above=None):
        """Mean of the last `seconds` if they all stayed within `tol` (and
        below / above the given limits) and we watched that long; else None."""
        now = time.time()
        if not self.vals or self.vals[0][0] > now - seconds + 0.25:
            return None                   # haven't watched long enough yet
        win = [v for t, v in self.vals if t >= now - seconds]
        if len(win) < 3 or max(win) - min(win) > tol:
            return None
        mean = sum(win) / len(win)
        if below is not None and abs(mean) >= below:
            return None
        if above is not None and mean <= above:
            return None
        return mean


def _no_progress(event):
    pass                 # nobody listening -> stay silent


class Reporter:
    """Small helper that turns 'say something' into progress(event) calls."""

    def __init__(self, vital, progress=None):
        self.vital = vital
        self.progress = progress or _no_progress

    def _send(self, phase, message=None, **extra):
        event = {"vital": self.vital, "phase": phase}
        if message is not None:
            event["message"] = message
        event.update(extra)
        self.progress(event)

    def info(self, message):
        self._send("info", message)

    def warn(self, message):
        self._send("warning", message)

    def tick(self, t_end, message, **live):
        """One countdown step while measuring (live = current values for the UI)."""
        left = round(max(0.0, t_end - time.time()), 1)
        if live:
            self._send("measuring", message, seconds_left=left, live=live)
        else:
            self._send("measuring", message, seconds_left=left)

    def end(self):
        self._send("countdown_end")

    def watching(self, kg, every=0.3):
        """While waiting for the person (auto-zero / step on): the live weight, at most every 0.3 s."""
        now = time.time()
        if kg is None or now - getattr(self, "_watch_t", 0) < every:
            return
        self._watch_t = now
        self._send("watching", live={"kg": round(kg, 2)})


# =====================================================================
# Small helpers
# =====================================================================
def calibrate_spo2(raw):
    """The sensor's SpO2 -> the value to show (SPO2_OFFSET added, never above 100 %)."""
    if raw is None:
        return None
    return min(SPO2_MAX, float(raw) + SPO2_OFFSET)


def trimmed_mean(values, trim=TRIM):
    if not values:
        return None
    v = sorted(values)
    k = int(len(v) * trim)
    if len(v) - 2 * k >= 3:
        v = v[k:len(v) - k]
    return sum(v) / len(v)


def median(vals):
    v = sorted(vals)
    return v[len(v) // 2] if v else None


def need(module_name, hint):
    """Import a hardware library, or explain what's missing."""
    try:
        return __import__(module_name)
    except ImportError as e:
        raise MeasureError(f"cannot import {module_name} ({e}). {hint}")


# =====================================================================
# 1) HEART RATE + SpO2 - MAX30102  -> average
# =====================================================================
def open_max30102(report=None):
    """Set up the MAX30102 (clone-safe). Returns (sensor, read_block, flush)."""
    R = report or Reporter("max30102")
    smbus = need("smbus", "Run on the Pi: sudo apt install python3-smbus")
    max30102 = need("max30102", f"max30102.py not found in {HW_DIR}")

    # part-ID check first, so a wiring problem gives a clear message
    bus = smbus.SMBus(I2C_BUS)
    try:
        part = bus.read_byte_data(MAX30102_ADDR, 0xFF)
    except OSError as e:
        raise MeasureError(f"MAX30102 not answering at 0x57 ({e}) - check wiring / i2cdetect")
    finally:
        bus.close()
    if part != MAX30102_PART_ID:
        R.warn(f"part ID is 0x{part:02X}, expected 0x15 (clone board?) - trying anyway")

    class SafeMAX30102(max30102.MAX30102):
        def reset(self):
            try:
                super().reset()
            except OSError:
                pass        # clone boards often don't ACK the reset write - harmless

    sensor = None
    for attempt in range(3):
        try:
            sensor = SafeMAX30102(channel=I2C_BUS, address=MAX30102_ADDR)
            break
        except OSError as e:
            R.warn(f"MAX30102 setup failed ({e}), retrying...")
            time.sleep(0.5)
    if sensor is None:
        raise MeasureError("MAX30102 setup failed 3 times - check power (3.3V) and SDA/SCL")

    # FIFO register addresses (MAX30102 datasheet)
    stats = {"samples": 0, "i2c_errors": 0, "stalls": 0, "t0": None}

    def status():
        """Raw register snapshot - tells us WHY no samples are coming."""
        try:
            rd = lambda reg: sensor.bus.read_byte_data(MAX30102_ADDR, reg)
            mode = rd(0x09)
            note = " (SHUTDOWN!)" if mode & 0x80 else (" (not in SpO2 mode)" if mode & 0x07 != 0x03 else "")
            return (f"MODE=0x{mode:02X}{note} WR_PTR={rd(0x04)} RD_PTR={rd(0x06)} "
                    f"OVF={rd(0x05)} FIFO_CFG=0x{rd(0x08):02X}")
        except OSError as e:
            return f"could not read registers ({e})"

    def flush():
        """Re-run the driver's own setup(): wakes the sensor and empties the FIFO.

        WHY: the sensor keeps sampling (~25 Hz) while we wait for the person.
        After ~1.3 s the 32-sample FIFO is full, and with rollover OFF (the
        driver's FIFO_CONFIG 0x4F) the write pointer freezes = read pointer,
        so "data present" reads 0 forever. setup() resets the pointers AND
        re-writes MODE_CONFIG, so a sensor left in shutdown wakes up too.
        """
        for _ in range(3):
            try:
                sensor.setup()
                stats["t0"] = time.time()
                return True
            except OSError:
                stats["i2c_errors"] += 1
                time.sleep(0.05)
        R.warn("MAX30102 setup() failed 3x - I2C problem (check 3.3V/SDA/SCL)")
        return False

    def read_block(n, timeout=None):
        """Read n samples, but never hang: give up after `timeout` seconds."""
        timeout = timeout or max(2.0, n / 25 * 3)      # ~3x the time n samples need
        red, ir = [], []
        t_stop = time.time() + timeout
        while len(ir) < n and time.time() < t_stop:
            try:
                avail = sensor.get_data_present()
                if not avail:
                    time.sleep(0.01)
                    continue
                for _ in range(min(avail, n - len(ir))):
                    r, i = sensor.read_fifo()
                    red.append(r)
                    ir.append(i)
            except OSError:
                stats["i2c_errors"] += 1
                time.sleep(0.05)
        stats["samples"] += len(ir)
        if len(ir) < n:                                # stalled -> show why, then restart
            stats["stalls"] += 1
            if stats["stalls"] == 1:
                R.warn(f"MAX30102 gave {len(ir)}/{n} samples in {timeout:.0f}s. "
                       f"Registers: {status()}")
            flush()
        return red, ir

    def report_line():
        """One line for the result table: how well the sensor delivered."""
        secs = time.time() - stats["t0"] if stats["t0"] else 0
        rate = stats["samples"] / secs if secs else 0
        return (f"{stats['samples']} in {secs:.0f}s = {rate:.1f}/s (expect ~25), "
                f"I2C errors {stats['i2c_errors']}, stalls {stats['stalls']}")

    read_block.report = report_line
    flush()
    return sensor, read_block, flush


def close_max30102(sensor):
    try:
        sensor.shutdown()
        sensor.bus.close()
    except OSError:
        pass


def measure_hr_spo2(confirm=None, progress=None):
    """Heart rate + SpO2 from the fingertip.

    Returns {"heart_rate_bpm", "hr_samples", "spo2_pct", "spo2_samples",
             "finger_detected", "ir_level", "sensor_report", "error"}
    (heart_rate_bpm / spo2_pct are None when there was no valid reading).
    """
    confirm = confirm or _no_confirm
    R = Reporter("hr", progress)
    hrcalc = need("hrcalc", f"hrcalc.py not found in {HW_DIR}")
    sensor, read_block, flush = open_max30102(R)

    hrs, spo2s = [], []
    no_finger = 0
    ir = []
    try:
        confirm("finger_on", "Place your fingertip gently on the sensor")
        flush()                                    # FIFO filled up while waiting - empty it
        R.info(f"Hold still - reading for {HR_SECONDS}s")
        red, ir = read_block(100)                  # first full 4 s window (25 Hz)
        t_end = time.time() + HR_SECONDS
        while time.time() < t_end:
            if len(ir) >= 100:
                if sum(ir[-100:]) / 100 < FINGER_IR_MIN:
                    no_finger += 1
                else:
                    hr, hr_ok, spo2, spo2_ok = hrcalc.calc_hr_and_spo2(ir[-100:], red[-100:])
                    if hr_ok and HR_RANGE[0] <= hr <= HR_RANGE[1]:
                        hrs.append(hr)
                    if spo2_ok and SPO2_RANGE[0] <= spo2 <= SPO2_RANGE[1]:
                        spo2s.append(spo2)
            R.tick(t_end, f"valid HR samples: {len(hrs):2d}", valid_samples=len(hrs))
            r, i = read_block(25)                  # slide the window by 1 s
            red, ir = (red + r)[-100:], (ir + i)[-100:]
        R.end()
    finally:
        close_max30102(sensor)

    avg_hr, avg_spo2 = trimmed_mean(hrs), trimmed_mean(spo2s)
    finger = not (avg_hr is None and no_finger)
    if not finger:
        R.warn("IR signal was too weak - finger not on the sensor?")
    error = None
    if avg_hr is None and avg_spo2 is None:
        error = ("no valid reading - finger not on the sensor?" if not finger
                 else "no valid reading - keep your finger still and try again")
    return {
        "heart_rate_bpm": float(avg_hr) if avg_hr is not None else None,
        "hr_samples": len(hrs),
        "spo2_pct": calibrate_spo2(avg_spo2),
        "spo2_raw_pct": float(avg_spo2) if avg_spo2 is not None else None,   # before SPO2_OFFSET - for calibrating
        "spo2_samples": len(spo2s),
        "finger_detected": finger,
        "ir_level": float(sum(ir) / len(ir)) if ir else None,
        "sensor_report": read_block.report(),
        "error": error,
    }


# =====================================================================
# 6) RESPIRATORY RATE - from the MAX30102 fingertip signal -> breaths/min
# =====================================================================
# Breathing slowly pushes the fingertip IR signal up and down (blood volume
# changes with chest pressure). Heartbeats are fast (1-2 Hz), breathing is
# slow (0.1-0.5 Hz), so a band-pass filter keeps just the breathing wave.
# The rate is then found two ways and the two must agree:
#   a) count the breath peaks in the filtered wave
#   b) FFT: the strongest frequency in the breathing band x 60
RR_MERGE_FRACTION = 0.5   # peaks closer than half a typical breath = one breath


def merge_close_peaks(peaks, wave, typical_gap=None):
    """Merge false double-peaks: two peaks much closer together than a normal
    breath are one breath with a bump on it - keep the taller one.

    "Normal breath" = typical_gap (samples) when given - the FFT's breathing
    period - otherwise the median gap. The FFT is used because with MANY bumps
    the median gap itself shrinks and the bumps no longer look "too close".
    (Real tests: bumps next to real breaths failed correct 7-9 breaths/min
    readings - unevenness 0.40 and 0.58, peak count 13.2 vs FFT 8.7.)
    """
    peaks = list(peaks)
    for _ in range(len(peaks)):
        if len(peaks) < 3:
            break
        gaps = [b - a for a, b in zip(peaks, peaks[1:])]
        typical = typical_gap or sorted(gaps)[len(gaps) // 2]
        k = min(range(len(gaps)), key=gaps.__getitem__)       # closest pair
        if gaps[k] >= RR_MERGE_FRACTION * typical:
            break
        a, b = peaks[k], peaks[k + 1]
        peaks.remove(a if wave[a] < wave[b] else b)          # drop the smaller one
    return peaks


def estimate_rr(ir, fs):
    np = need("numpy", "pip install numpy")
    x = np.asarray(ir, dtype=float)
    n = len(x)
    t = np.arange(n) / fs
    x = x - np.polyval(np.polyfit(t, x, 1), t)        # remove slow sensor drift

    # band-pass with an FFT: keep only waves at breathing speed
    X = np.fft.rfft(x)
    f = np.fft.rfftfreq(n, 1 / fs)
    X[(f < RR_BAND_HZ[0]) | (f > RR_BAND_HZ[1])] = 0
    wave = np.fft.irfft(X, n)

    # a) peak counting (skip the filter's wobbly edges)
    edge = int(RR_EDGE_SEC * fs)
    min_gap = int(fs * 60 / RR_RANGE[1])               # breaths can't be closer than this
    core = wave[edge:n - edge]
    thr = 0.3 * np.std(core) if len(core) else 0
    peaks = []
    for i in range(max(edge, 1), n - edge - 1):
        lo, hi = max(0, i - min_gap // 2), min(n, i + min_gap // 2 + 1)
        if wave[i] > thr and wave[i] == wave[lo:hi].max():
            if not peaks or i - peaks[-1] >= min_gap:
                peaks.append(i)

    # b) FFT of the breathing wave (zero-padded for a finer answer)
    pad = 8 * n
    # spectrum of the drift-removed signal BEFORE the hard band cut: the cut itself makes a
    # false bump at 8/min when the strong ~6/min blood-vessel wave sits just below it
    spec = np.abs(np.fft.rfft(x * np.hanning(n), pad))
    fp = np.fft.rfftfreq(pad, 1 / fs)
    band = (fp >= RR_BAND_HZ[0]) & (fp <= RR_BAND_HZ[1])
    # strongest REAL peak inside the band (a hump that only looks big because it sits on
    # the band edge, i.e. leakage from the ~6/min blood-vessel wave, does not count)
    rr_fft = None
    sb, fb = spec[band], fp[band]
    tops = [i for i in range(1, len(sb) - 1) if sb[i] > sb[i - 1] and sb[i] >= sb[i + 1]]
    if tops:
        rr_fft = float(fb[max(tops, key=sb.__getitem__)] * 60)
    power = spec[band] ** 2
    top_power = power[max(tops, key=sb.__getitem__)] if tops else 0.0
    strength = float(top_power / power.mean()) if band.any() else 0.0     # one clear rhythm?

    # clean up the peaks using the FFT's rhythm - but only if that rhythm is clear
    # (a near-floor FFT answer is usually the blood-vessel wave - letting it guide the merge
    #  would glue real breaths together and drag the peak count down to match it)
    fft_gap = (fs * 60 / rr_fft if rr_fft and rr_fft >= RR_FFT_MIN_TRUST
               and strength >= RR_MIN_STRENGTH_AGREE else None)
    found = len(peaks)
    peaks = merge_close_peaks(peaks, wave, fft_gap)
    merged = found - len(peaks)
    rr_peaks = None
    if len(peaks) >= 3:
        rr_peaks = 60 / float(np.median(np.diff(peaks))) * fs

    # quality checks
    gaps = np.diff(peaks)
    steadiness = float(gaps.std() / gaps.mean()) if len(gaps) > 1 else 9.9  # 0 = perfectly even
    raw = np.asarray(ir, dtype=float)
    k = int(fs)
    blocks = raw[:len(raw) // k * k].reshape(-1, k).mean(axis=1)            # 1-second averages
    jump = float(np.abs(np.diff(blocks)).max() / np.median(raw)) if len(blocks) > 1 else 0.0

    return {"rr_peaks": rr_peaks, "rr_fft": rr_fft, "peaks": peaks, "merged": merged,
            "strength": strength, "steadiness": steadiness, "jump": jump,
            "wave": wave, "t": t, "ir": raw}


def save_rr_log(res, rr):
    """CSV of the raw + filtered signal, and a PNG plot if matplotlib is installed."""
    os.makedirs(RR_LOG_DIR, exist_ok=True)
    stem = os.path.join(RR_LOG_DIR, time.strftime("%Y%m%d_%H%M%S") + "_rr")
    with open(stem + ".csv", "w") as f:
        f.write("t_s,ir_raw,breathing_wave,is_breath_peak\n")
        peaks = set(res["peaks"])
        for i, (t, raw, w) in enumerate(zip(res["t"], res["ir"], res["wave"])):
            f.write(f"{t:.3f},{raw:.0f},{w:.2f},{int(i in peaks)}\n")
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return stem + ".csv"
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(10, 5), sharex=True)
    a1.plot(res["t"], res["ir"], lw=0.8)
    a1.set_ylabel("raw IR")
    a1.set_title("MAX30102 fingertip signal")
    a2.plot(res["t"], res["wave"], lw=1.2)
    pk = res["peaks"]
    a2.plot(res["t"][pk], res["wave"][pk], "o")
    a2.set_ylabel("breathing wave")
    a2.set_xlabel("seconds")
    a2.set_title(f"{len(pk)} breaths found -> {rr}")
    fig.tight_layout()
    fig.savefig(stem + ".png", dpi=110)
    plt.close(fig)
    return stem + ".png"


# Result levels (tuned 2026-10-27 on 2 real recordings + 1000s of simulated ones):
#   CONFIRMED - all checks pass                     -> about 1-2% are >2 breaths/min off
#   ESTIMATE  - checks just missed, but both methods agree closely and the
#               signal is still clear               -> typically within ~1 breath/min
#   NONE      - too faint / moved / methods disagree -> showing a number here
#               would often be wrong, so we don't
RR_EST_AGREE = 2.0        # estimate: peak count and FFT within this many breaths/min
RR_EST_STRENGTH = 5.5     # estimate: breathing peak at least this clear
RR_EST_UNEVEN = 0.35      # estimate: breaths at most this uneven (was 0.30)
# Relaxed fallbacks (2026-10-31, capstone demo: fewer "try again" for patients).
# Tested on 20 hand-counted runs - see the note in classify_rr. Set RR_RELAXED = False
# to go back to the strict rules.
RR_RELAXED = True
RR_RELAX_AGREE = 2.0      # A) both methods within this many breaths/min -> accept, even if faint/uneven
RR_RELAX_MAX_JUMP = 0.08  # ...and the finger moved less than this (strict limit is 5%)
RR_RELAX_MIN_STRENGTH = 2.5   # ...and the rhythm is at least this clear (strict: 4-6)
RR_PEAK_ONLY_UNEVEN = 0.30    # B) FFT unusable: trust the peak count alone if breaths are this even
RR_PEAK_ONLY_MIN_BREATHS = round(8 * RR_SECONDS / 60)  # ...and at least this many breaths were
                              # found (8 per 60 s, scaled to the window: 30 s -> 4)


def classify_rr(res):
    """Decide CONFIRMED / ESTIMATE / NONE and explain why in plain words."""
    a, b = res["rr_peaks"], res["rr_fft"]
    ok = lambda v: v is not None and RR_RANGE[0] <= v <= RR_RANGE[1]
    both = ok(a) and ok(b)
    # relaxed strength only when the two methods agreed WITHOUT help: after
    # FFT-guided merging they agree almost by construction, so that's no proof
    tight = both and abs(a - b) <= RR_TIGHT_AGREE and res["merged"] == 0
    need = RR_MIN_STRENGTH_AGREE if tight else RR_MIN_STRENGTH
    out = {"level": "NONE", "rr": None, "low": None, "high": None,
           "reasons": [], "need_strength": need}

    reasons = []
    if res["jump"] > RR_MAX_JUMP:
        reasons.append(f"the finger moved or pressed harder (signal jumped {res['jump'] * 100:.0f}%)")
    if not both and a is not None and b is not None:   # (a None = too few breaths, said below)
        shown = " / ".join(f"{v:.1f}" for v in (a, b) if v is not None) or "none"
        reasons.append(f"no breathing rate in the normal {RR_RANGE[0]}-{RR_RANGE[1]}/min range (got {shown})")
    elif both and abs(a - b) > RR_AGREE:
        reasons.append(f"the two methods disagree ({a:.1f} vs {b:.1f}/min)")
    if b is not None and b < RR_FFT_MIN_TRUST:
        reasons.append(f"the FFT rhythm ({b:.1f}/min) is too close to the slow blood-vessel wave to trust")
    if res["strength"] < need:
        reasons.append(f"the breathing signal is faint (strength {res['strength']:.1f}, need {need})")
    npk = len(res.get("peaks", [0, 0, 0]))
    if npk < 3:
        reasons.append(f"only {npk} breath(s) found - too few to measure")
    elif res["steadiness"] > RR_MAX_UNEVEN:
        reasons.append(f"the breaths look uneven ({res['steadiness']:.2f}, limit {RR_MAX_UNEVEN})")
    out["reasons"] = reasons

    if not reasons:
        out.update(level="CONFIRMED", rr=(a + b) / 2)
    elif (res["jump"] <= RR_MAX_JUMP and both and abs(a - b) <= RR_EST_AGREE and b >= RR_FFT_MIN_TRUST
          and res["strength"] >= RR_EST_STRENGTH and res["steadiness"] <= RR_EST_UNEVEN):
        rr = (a + b) / 2
        out.update(level="ESTIMATE", rr=rr,
                   low=round(min(a, b, rr - 1)), high=round(max(a, b, rr + 1)))
    elif RR_RELAXED and res["jump"] <= RR_RELAX_MAX_JUMP:
        # A) the two methods agree closely - accept even if the signal is faint or a bit uneven
        if (both and abs(a - b) <= RR_RELAX_AGREE and b >= RR_FFT_MIN_TRUST
                and res["strength"] >= RR_RELAX_MIN_STRENGTH and npk >= 5):
            rr = (a + b) / 2
            out.update(level="ESTIMATE", rr=rr, basis="both methods agree (relaxed)",
                       low=round(min(a, b, rr - 1)), high=round(max(a, b, rr + 1)))
        # B) FFT unusable or disagrees, but the counted breaths are steady: use the peak count
        elif (ok(a) and a >= RR_FFT_MIN_TRUST and npk >= RR_PEAK_ONLY_MIN_BREATHS
              and res["steadiness"] <= RR_PEAK_ONLY_UNEVEN):
            out.update(level="ESTIMATE", rr=a, basis="peak count only (relaxed)",
                       low=round(a - 1.5), high=round(a + 1.5))
    return out


def rr_summary(result):
    """Short human text for an RR result (used for the plot title and the CLI)."""
    if result["level"] == "CONFIRMED":
        return f"{result['rr_bpm']:.0f} breaths/min"
    if result["level"] == "ESTIMATE":
        return (f"~{result['rr_bpm']:.0f} breaths/min "
                f"(estimate, likely {result['low']}-{result['high']})")
    return "no reliable reading"


class _FingerWatch:
    """While the screen waits for the finger: reads the sensor in small blocks
    and says when a finger has been ON and STEADY for FINGER_STEADY_SEC.
    Also tells the screen live whether a finger is there (green / waiting),
    and spots a dead sensor (IR stuck at 0 / maximum) so the patient isn't blamed."""

    def __init__(self, read_block, R):
        self.read_block, self.R = read_block, R
        self.reset()

    def reset(self):
        self.on_since = None
        self.levels = []                  # (time, mean IR) while the finger is on
        self.dead_since = None
        self._sent = 0.0

    def ready(self):
        _red, ir = self.read_block(5, timeout=1.0)         # ~0.2 s
        now = time.time()
        if _dead_block(ir):
            self.dead_since = self.dead_since or now
            if now - self.dead_since >= SENSOR_DEAD_SEC:
                raise MeasureError("the finger sensor gives no signal (IR stuck at 0 / maximum) "
                                   "- check the MAX30102 wiring")
        else:
            self.dead_since = None
        mean = sum(ir) / len(ir) if ir else 0
        on = mean >= FINGER_IR_MIN
        if not on:
            self.on_since, self.levels = None, []
        else:
            self.on_since = self.on_since or now
            self.levels.append((now, mean))
            self.levels = [(t, v) for t, v in self.levels if t >= now - FINGER_STEADY_SEC]
        vals = [v for _t, v in self.levels]
        if on and len(vals) > 1 and (max(vals) - min(vals)) / max(vals) >= FINGER_STEADY_FRAC:
            self.on_since, self.levels = now, [(now, mean)]   # it moved - steady time starts again
        steady_s = now - self.on_since if self.on_since else 0.0
        steady = on and steady_s >= FINGER_STEADY_SEC
        if now - self._sent >= 0.3 or steady:
            self._sent = now
            self.R._send("watching", live={"finger": on, "steady_s": round(min(steady_s, FINGER_STEADY_SEC), 1)})
        return steady


def _dead_block(ir):
    """A block with no samples, or only exact 0 / saturated values = the sensor isn't working."""
    return not ir or all(v == 0 or v >= IR_SATURATED for v in ir)


def _record_finger(R, confirm, seconds, step, message):
    """Record `seconds` of fingertip signal, keeping BOTH light channels.

    The MAX30102 always gives red + infrared together (~25 samples/s). Nothing
    is calculated while recording, so the sample rate stays steady; the
    analysis runs afterwards.

    Live finger detection (Nov 2026):
      - the reading starts by itself once a finger is on + steady
        (FINGER_STEADY_SEC); the screen's button is only a backup
      - every ~0.5 s the screen hears: finger on? moving? how long off?
      - ONE lift longer than FINGER_OFF_MAX_SEC -> this recording can't be used,
        so it restarts RIGHT AWAY (no waiting out the 30 s), at most
        FINGER_MAX_RESTARTS times
      - IR stuck at 0 / maximum for SENSOR_DEAD_SEC -> MeasureError (sensor
        problem, not the patient's fault)
    Returns (red, ir, elapsed_s, longest_lift_s, sensor_report, info) where
    info = {"restarts", "finger_wait_s", "gave_up"}.
    """
    sensor, read_block, flush = open_max30102(R)
    info = {"restarts": 0, "finger_wait_s": None, "gave_up": False}
    red, ir, elapsed, longest_off = [], [], 0.0, 0.0
    t_prompt = time.time()
    try:
        watch = _FingerWatch(read_block, R)
        while True:
            watch.reset()
            _confirm_auto(confirm, step, message, watch.ready)
            if info["finger_wait_s"] is None:
                info["finger_wait_s"] = round(time.time() - t_prompt, 1)   # how long people need (tuning)
            flush()                                    # FIFO filled up while waiting - empty it
            R.info(f"Don't talk or move - reading for {seconds}s")
            read_block(25)                             # drop the first second (settling)
            red, ir = [], []
            off_run = longest = 0                      # samples in the current / longest lift
            prev_mean, move_until, dead = None, 0.0, 0.0
            restart = False
            t0 = time.time()
            t_end = t0 + seconds
            while time.time() < t_end:
                tb = time.time()
                r, i = read_block(FINGER_BLOCK)
                now = time.time()
                if _dead_block(i):
                    dead += now - tb
                    if dead >= SENSOR_DEAD_SEC:
                        raise MeasureError("the finger sensor gives no signal (IR stuck at 0 / maximum) "
                                           "- check the MAX30102 wiring")
                    continue
                dead = 0.0
                for v in i:
                    off_run = off_run + 1 if v < FINGER_IR_MIN else 0
                    longest = max(longest, off_run)
                mean = sum(i) / len(i)
                on = mean >= FINGER_IR_MIN
                if on and prev_mean and abs(mean - prev_mean) / prev_mean > FINGER_MOVE_FRAC:
                    move_until = now + 1.5                 # keep the hint up a moment
                prev_mean = mean if on else None
                red += r
                ir += i
                off_s = off_run / 25
                R.tick(t_end, f"samples: {len(ir):4d}", samples=len(ir), finger=on,
                       moving=on and now < move_until, off_s=round(off_s, 1))
                if off_s > FINGER_OFF_MAX_SEC:
                    restart = True                     # this recording can't succeed any more
                    break
            elapsed = time.time() - t0
            longest_off = longest / 25
            R.end()
            if not restart:
                break
            if info["restarts"] >= FINGER_MAX_RESTARTS:
                info["gave_up"] = True
                break
            info["restarts"] += 1
            R._send("restart", f"finger off for more than {FINGER_OFF_MAX_SEC:.0f}s - starting again",
                    restart=info["restarts"])
            step = "finger_again"
            message = "Put your finger back on the sensor - the reading starts again by itself"
    finally:
        close_max30102(sensor)
    sensor_report = read_block.report()
    R.info(f"Sensor samples: {sensor_report}")
    return red, ir, elapsed, longest_off, sensor_report, info


def _recording_problem(ir, longest_off_s, info=None):
    """Text if the recording itself is unusable, else None."""
    if info and info.get("gave_up"):
        return "no steady reading - the finger kept leaving the sensor"
    if longest_off_s > FINGER_OFF_MAX_SEC:
        return "no reading - finger left the sensor (IR too weak)"
    if len(ir) < 20 * 25:
        return f"no reading - only {len(ir)} samples (I2C errors?)"
    return None


def _analyse_rr(R, ir, elapsed, sensor_report, save_log):
    """Respiratory rate from a finished IR recording (see measure_rr)."""
    fs = len(ir) / elapsed                         # real sample rate (should be ~25 Hz)
    if not 15 <= fs <= 35:
        R.warn(f"odd sample rate {fs:.1f} Hz - using 25 Hz")
        fs = 25.0

    res = estimate_rr(ir, fs)
    c = classify_rr(res)
    result = {
        "rr_bpm": float(c["rr"]) if c["rr"] is not None else None,
        "level": c["level"],
        "low": c["low"],
        "high": c["high"],
        "reasons": c["reasons"],
        "need_strength": c["need_strength"],
        "rr_peaks": res["rr_peaks"],
        "rr_fft": res["rr_fft"],
        "peaks_count": len(res["peaks"]),
        "merged": res["merged"],
        "strength": res["strength"],
        "steadiness": res["steadiness"],
        "jump": res["jump"],
        "duration_s": len(ir) / fs,
        "sensor_report": sensor_report,
        "log_path": None,
        "error": None if c["level"] != "NONE" else "no reliable reading",
    }
    if save_log:
        result["log_path"] = save_rr_log(res, rr_summary(result))
    return result


def _analyse_hr_spo2(red, ir):
    """Heart rate + SpO2 from a finished recording: run hrcalc on every 4 s
    window (100 samples), sliding 1 s (25 samples) at a time - the same
    window, filters and trimmed average as measure_hr_spo2(), just over the
    whole recording instead of 20 s."""
    hrcalc = need("hrcalc", f"hrcalc.py not found in {HW_DIR}")
    hrs, spo2s, windows, no_finger = [], [], 0, 0
    for start in range(0, len(ir) - 100 + 1, 25):
        w_ir, w_red = ir[start:start + 100], red[start:start + 100]
        windows += 1
        if sum(w_ir) / 100 < FINGER_IR_MIN:
            no_finger += 1
            continue
        hr, hr_ok, spo2, spo2_ok = hrcalc.calc_hr_and_spo2(w_ir, w_red)
        if hr_ok and HR_RANGE[0] <= hr <= HR_RANGE[1]:
            hrs.append(hr)
        if spo2_ok and SPO2_RANGE[0] <= spo2 <= SPO2_RANGE[1]:
            spo2s.append(spo2)
    avg_hr, avg_spo2 = trimmed_mean(hrs), trimmed_mean(spo2s)
    return {
        "heart_rate_bpm": float(avg_hr) if avg_hr is not None else None,
        "hr_samples": len(hrs),
        "spo2_pct": calibrate_spo2(avg_spo2),
        "spo2_raw_pct": float(avg_spo2) if avg_spo2 is not None else None,   # before SPO2_OFFSET - for calibrating
        "spo2_samples": len(spo2s),
        "windows": windows,
        "no_finger_windows": no_finger,
    }


def measure_rr(confirm=None, progress=None, save_log=True):
    """Respiratory rate from the fingertip signal (RR_SECONDS, now 30 s).

    Returns {"rr_bpm", "level" (CONFIRMED/ESTIMATE/NONE), "low", "high",
             "reasons", ... quality numbers ..., "log_path", "error"}.
    If the recording itself failed (finger left, too few samples) only
    {"rr_bpm": None, "level": "NONE", "sensor_report", "error"} is returned.
    """
    confirm = confirm or _no_confirm
    R = Reporter("rr", progress)
    red, ir, elapsed, longest_off, report, info = _record_finger(
        R, confirm, RR_SECONDS, "finger_rest",
        "Rest your fingertip on the sensor, sit still, breathe normally")
    problem = _recording_problem(ir, longest_off, info)
    if problem:
        return {"rr_bpm": None, "level": "NONE", "sensor_report": report, "error": problem, **info}
    result = _analyse_rr(R, ir, elapsed, report, save_log)
    result.update(info)
    return result


def measure_finger(confirm=None, progress=None, save_log=True):
    """ONE finger reading for three vitals: respiratory rate, SpO2 and heart rate.

    Records RR_SECONDS (30 s) of red + infrared, THEN calculates:
      - respiratory rate from the IR breathing wave (same as measure_rr)
      - heart rate + SpO2 with hrcalc over every 4 s window (more windows than
        the 20 s measure_hr_spo2, so steadier averages)

    Returns everything measure_rr returns, plus "heart_rate_bpm",
    "hr_samples", "spo2_pct", "spo2_samples", and an overall "error"
    (None only when RR, HR and SpO2 all have a reading).
    """
    confirm = confirm or _no_confirm
    R = Reporter("finger", progress)
    red, ir, elapsed, longest_off, report, info = _record_finger(
        R, confirm, RR_SECONDS, "finger_rest",
        "Rest your fingertip on the sensor, sit still and breathe normally")
    problem = _recording_problem(ir, longest_off, info)
    if problem:
        return {"rr_bpm": None, "level": "NONE", "heart_rate_bpm": None, "spo2_pct": None,
                "hr_samples": 0, "spo2_samples": 0, "sensor_report": report,
                "rr_error": problem, "error": problem, **info}

    result = _analyse_rr(R, ir, elapsed, report, save_log)
    result.update(info)
    result.update(_analyse_hr_spo2(red, ir))
    result["rr_error"] = result["error"]
    missing = []
    if result["level"] == "NONE":
        missing.append("respiratory rate")
    if result["heart_rate_bpm"] is None:
        missing.append("heart rate")
    if result["spo2_pct"] is None:
        missing.append("SpO2")
    result["error"] = (f"no reliable {' / '.join(missing)} - keep your finger still and try again"
                       if missing else None)
    return result


# =====================================================================
# 2) HEIGHT - VL53L0X  -> average
# =====================================================================
def measure_height(confirm=None, progress=None, mount_mm=None, auto_after=None):
    """Distance from the overhead sensor to the top of the head.

    Returns {"distance_mm", "samples", "height_cm", "error"}.
    height_cm is None while SENSOR_MOUNT_MM (or mount_mm) isn't set.
    auto_after: start by itself after this many seconds (BMI flow - the person
    is already standing on the platform); the button stays as a backup.
    """
    confirm = confirm or _no_confirm
    R = Reporter("height", progress)
    mount_mm = SENSOR_MOUNT_MM if mount_mm is None else mount_mm
    board = need("board", "pip install adafruit-blinka (run on the Pi)")
    busio = need("busio", "pip install adafruit-blinka (run on the Pi)")
    vl53 = need("adafruit_vl53l0x", "pip install adafruit-circuitpython-vl53l0x")

    i2c = busio.I2C(board.SCL, board.SDA)
    samples = []
    try:
        tof = vl53.VL53L0X(i2c, io_timeout_s=TOF_IO_TIMEOUT_S)   # never wait forever
        tof.measurement_timing_budget = TOF_TIMING_BUDGET_US
        if auto_after:
            t0 = time.time()

            def ready():
                time.sleep(0.1)
                return time.time() - t0 >= auto_after

            _confirm_auto(confirm, "stand_under", "Stand straight under the sensor", ready)
        else:
            confirm("stand_under", "Stand straight under the sensor")
        R.info(f"Stand still - measuring for {HEIGHT_SECONDS}s")
        t_end = time.time() + HEIGHT_SECONDS
        while time.time() < t_end:
            try:
                d = tof.range
            except (OSError, RuntimeError):            # I2C error / "Timeout waiting for VL53L0X!"
                continue
            if TOF_RANGE_MM[0] < d < TOF_RANGE_MM[1]:
                samples.append(d)
            live_cm = (round((mount_mm - TOF_SCALE * samples[-1]) / 10, 1)
                       if samples and mount_mm else None)
            R.tick(t_end, f"samples: {len(samples):3d}", samples=len(samples), cm=live_cm)
        R.end()
    finally:
        i2c.deinit()                               # free the bus for the next sensor

    raw = trimmed_mean(samples)
    if raw is None:
        return {"distance_mm": None, "raw_mm": None, "spread_mm": None, "samples": 0,
                "height_cm": None, "error": "no valid reading (nothing under the sensor?)"}
    mean = sum(samples) / len(samples)
    spread = (sum((s - mean) ** 2 for s in samples) / len(samples)) ** 0.5
    dist = TOF_SCALE * raw                         # calibrated distance
    return {
        "distance_mm": float(dist),
        "raw_mm": float(raw),                      # uncorrected sensor average (for calibration)
        "spread_mm": float(spread),                # sample std-dev = how noisy it was
        "samples": len(samples),
        "height_cm": (mount_mm - dist) / 10 if mount_mm else None,
        "error": None,
    }


# =====================================================================
# 3) WEIGHT - ESP32 + HX711 + load cells  -> median of the steady readings
# =====================================================================
# The ESP32 (hardware/scale_esp32/scale_esp32.ino) is the only thing that
# reads the HX711 - the Pi's Python timing corrupted HX711 reads, the ESP32's
# doesn't. It sends "W:<kg>" 5x per second over USB and takes commands:
#   T = tare, C:<kg> = calibrate, S = save, F? / F:<x> = get/set factor.
class ScaleLink:
    """USB serial link to the ESP32 scale.

    Opening the port restarts the ESP32 (~8 s), so a long-running program
    (the kiosk agent) should open it ONCE and keep it open.
    """

    def __init__(self, port):
        serial = need("serial", "pip install pyserial")
        self.port = port
        self.ser = serial.Serial(port, SCALE_BAUD, timeout=0.1)
        self.buf = b""
        self.ready_factor = None
        # auto-zero bookkeeping (see watch_zero / zero_trusted)
        self.last_kg = None
        self.zero_ok_at = None            # last time the platform was seen empty at ~0 kg
        self.zero_note = "not checked yet since the scale was connected"
        self.idle = SteadyWatch()

    def close(self):
        try:
            self.ser.close()
        except OSError:
            pass

    def send(self, cmd):
        self.ser.write((cmd + "\n").encode())

    def drain(self):
        """Throw away old W: lines that piled up while nobody was reading."""
        self.ser.reset_input_buffer()
        self.buf = b""

    def clear_line(self):
        """Push leftover junk out of the ESP32's command buffer.

        On the Pi, opening the port does NOT restart the ESP32, so half a line
        of junk can still be waiting there; the next command then arrives as
        e.g. "xT" -> "ERR:unknown command". An empty newline makes the ESP32
        finish that junk line on its own (empty lines are ignored by the
        sketch). We then wait for its possible ERR: reply and throw it away,
        so it can't be mistaken for an answer to the real command.
        """
        self.ser.write(b"\n")
        time.sleep(0.3)
        self.drain()

    def lines(self, seconds):
        """Yield every line the ESP32 sends for `seconds`."""
        t_end = time.time() + seconds
        while time.time() < t_end:
            self.buf += self.ser.read(256)
            while b"\n" in self.buf:
                raw, self.buf = self.buf.split(b"\n", 1)
                line = raw.decode(errors="ignore").strip()
                if line:
                    yield line

    def read_kg(self, seconds):
        """Every W: reading (kg) that arrives within `seconds` (short = non-blocking-ish)."""
        vals = []
        for line in self.lines(seconds):
            if line.startswith("W:"):
                try:
                    vals.append(float(line[2:]))
                except ValueError:
                    continue
            elif line.startswith("ERR:"):
                raise MeasureError(f"ESP32 scale: {line[4:]}")
        if vals:
            self.last_kg = vals[-1]
        return vals

    # ---------------- auto-zero ----------------
    def zero_trusted(self):
        return self.zero_ok_at is not None and time.time() - self.zero_ok_at < ZERO_FRESH_SEC

    def zero_age(self):
        return None if self.zero_ok_at is None else time.time() - self.zero_ok_at

    def mark_zero_bad(self, why):
        self.zero_ok_at = None
        self.zero_note = why

    def verify_zero(self):
        """Right after a tare: the reading must stay ~0 for ZERO_VERIFY_SEC.
        If it jumps, someone stepped on while it tared -> that zero is wrong."""
        self.drain()
        vals = self.read_kg(ZERO_VERIFY_SEC + 0.3)
        if vals and all(abs(v) <= ZERO_VERIFY_TOL_KG for v in vals):
            self.zero_ok_at = time.time()
            self.zero_note = "zeroed"
            return True
        self.mark_zero_bad("the reading moved right after zeroing (someone stepped on?)")
        return False

    def idle_reset(self):
        """Call when the agent becomes idle again: old readings piled up meanwhile."""
        self.drain()
        self.idle.clear()

    def watch_zero(self):
        """Call often while the kiosk is idle. Keeps the zero fresh by itself:
        an empty + steady platform near 0 confirms the zero (or re-tares it when
        it drifted a little). Returns a short log text when something changed."""
        for v in self.read_kg(0.3):
            self.idle.add(v)
        mean = self.idle.steady(EMPTY_STEADY_SEC, STEADY_TOL_KG)
        if mean is None:
            return None                   # moving / not watched long enough
        self.idle.clear()                 # judge the next few seconds afresh
        if abs(mean) <= ZERO_OK_KG:
            was_bad = self.zero_ok_at is None
            self.zero_ok_at = time.time()
            self.zero_note = "zero confirmed while idle"
            return f"auto-zero: platform empty, zero OK ({mean:+.2f} kg)" if was_bad else None
        if abs(mean) <= EMPTY_MAX_KG:     # empty but drifted a little -> re-tare
            self.tare()
            ok = self.verify_zero()
            return (f"auto-zero: re-tared (drifted {mean:+.2f} kg)" if ok
                    else "auto-zero: tare disturbed - zero NOT trusted")
        if abs(mean) < PERSON_MIN_KG:     # something light left on it, or a big drift
            if self.zero_ok_at is not None:
                self.mark_zero_bad(f"steady {mean:+.1f} kg while idle (bag on it / drift?)")
                return f"auto-zero: steady {mean:+.1f} kg on an idle platform - zero NOT trusted"
            return None
        return None                       # a person standing on it - leave the zero alone

    def wait_for(self, prefix, seconds):
        """Return the text after `prefix`, or None on timeout. ERR: lines raise."""
        for line in self.lines(seconds):
            if line.startswith("ERR:"):
                raise MeasureError(f"ESP32 scale: {line[4:]}")
            if line.startswith(prefix):
                return line[len(prefix):].strip()
        return None

    def connect(self):
        """The scale is ready when it either:
          - says READY:SCALE (it just restarted when the port opened), or
          - is already streaming W: weights (it did NOT restart - this is what
            the CH340 board does on the Pi, so there is no READY line at all).
        PING is only the last resort, because the scale sketch currently on the
        ESP32 doesn't know PING (it answers "ERR: unknown command PING")."""
        ready = False
        for line in self.lines(SCALE_BOOT_SEC):
            if line.startswith("READY:SCALE"):
                if "factor=" in line:
                    try:
                        self.ready_factor = float(line.split("factor=")[1])
                    except ValueError:
                        pass
                ready = True
                break
            if line.startswith("W:"):
                ready = True
                break
        if not ready:
            self.clear_line()
            self.send("PING")
            if self.wait_for("PONG:SCALE", 2) is None:
                raise MeasureError(f"no ESP32 scale answering on {self.port} - "
                                   "is scale_esp32.ino uploaded? Serial Monitor closed?")
        self.clear_line()                  # also drains

    def tare(self):
        self.send("T")
        if self.wait_for("OK:TARE", 10) is None:
            raise MeasureError("ESP32 scale did not finish the tare (10 s)")

    def factor(self):
        self.send("F?")
        v = self.wait_for("FACTOR:", 3)
        return float(v) if v else None

    def weights(self, seconds, report=None, label="now"):
        """Collect W: readings (kg) for `seconds`, reporting a live countdown."""
        R = report or Reporter("weight")
        vals = []
        t_end = time.time() + seconds
        for line in self.lines(seconds):
            if line.startswith("W:"):
                try:
                    vals.append(float(line[2:]))
                except ValueError:
                    continue
                R.tick(t_end, f"{label}: {vals[-1]:8.2f} kg", kg=vals[-1])
            elif line.startswith("ERR:"):
                raise MeasureError(f"ESP32 scale: {line[4:]}")
        R.end()
        return vals


def find_scale_port(port=None):
    """`port` (or SCALE_PORT) if given, else ask every ESP32-looking USB port 'PING'."""
    if port:
        if port.startswith("/dev/") and not os.path.exists(port):
            raise MeasureError(f"scale ESP32 not found at {port} - "
                               "is it plugged in? (check: ls -l /dev/serial/by-id/)")
        return port
    try:
        from serial.tools import list_ports
    except ImportError:
        raise MeasureError("pip install pyserial")
    ports = [p.device for p in list_ports.comports() if p.vid in ESP32_USB_IDS]
    if not ports:
        raise MeasureError("no ESP32 found on USB - plug in the scale ESP32 "
                           "(or give --scale-port /dev/ttyUSB0)")
    if len(ports) == 1:
        return ports[0]
    serial = need("serial", "pip install pyserial")
    for p in ports:                        # several boards: find the one that says PONG:SCALE
        try:
            with serial.Serial(p, SCALE_BAUD, timeout=0.1) as s:
                time.sleep(SCALE_BOOT_SEC)
                s.reset_input_buffer()
                s.write(b"PING\n")
                t_end = time.time() + 2
                while time.time() < t_end:
                    if b"PONG:SCALE" in s.readline():
                        return p
        except OSError:
            continue
    raise MeasureError(f"none of {ports} answered as the scale - use --scale-port")


def open_scale(port=None, progress=None):
    """Find and connect to the scale ESP32. Remember to link.close() it."""
    R = Reporter("weight", progress)
    port = find_scale_port(port)
    R.info(f"Scale: ESP32 on {port} - connecting (it restarts, ~{SCALE_BOOT_SEC}s)...")
    link = ScaleLink(port)
    try:
        link.connect()
    except Exception:
        link.close()
        raise
    return link


def measure_weight(link, confirm=None, progress=None):
    """Weigh the person. `link` is an open ScaleLink (see open_scale()).

    Normal kiosk flow (auto-zero): the zero was kept fresh while the kiosk was
    idle, the patient is already standing on the platform -> no "step off";
    weighing starts as soon as they stand still.
    Fallback (zero not trusted): "step off" -> tares by itself once the
    platform is empty -> "step on" -> starts by itself once they stand still.
    The confirm buttons stay as a backup for both prompts.

    Returns {"weight_kg", "steadiness_kg", "readings", "zero", "error"}.
    """
    confirm = confirm or _no_confirm
    R = Reporter("weight", progress)
    link.clear_line()          # the agent keeps the port open for hours - clear junk first

    zero = "kept"
    if AUTO_ZERO and link.zero_trusted():
        R.info(f"Zero checked {link.zero_age():.0f}s ago - no need to step off.")
    else:
        R.info(f"Zero not trusted ({link.zero_note}) - asking to step off.")
        empty = SteadyWatch()

        def platform_empty():
            for v in link.read_kg(0.1):
                empty.add(v)
            R.watching(link.last_kg)
            return empty.steady(FALLBACK_STEADY_SEC, STEADY_TOL_KG, below=FALLBACK_EMPTY_MAX_KG) is not None

        _confirm_auto(confirm, "scale_empty",
                      "Step OFF the platform - it zeroes by itself once it is empty", platform_empty)
        link.tare()
        if not link.verify_zero():
            return {"weight_kg": None, "steadiness_kg": None, "readings": 0, "zero": "failed",
                    "error": "the platform moved while it was zeroing - step off and try again"}
        R.info("Tared.")
        zero = "now"

    on = SteadyWatch()

    def standing_still():
        for v in link.read_kg(0.1):
            on.add(v)
        R.watching(link.last_kg)
        return on.steady(PERSON_STEADY_SEC, PERSON_STEADY_TOL_KG, above=PERSON_MIN_KG) is not None

    # Normal flow: they never left the platform - if they stand still, just weigh.
    link.drain()
    already_on = False
    if zero == "kept":
        t_end = time.time() + PERSON_STEADY_SEC + 0.8
        while time.time() < t_end and not already_on:
            already_on = standing_still()
    if not already_on:
        _confirm_auto(confirm, "step_on", "Step ONTO the platform and stand still", standing_still)
        # While the screen waited, the ESP32 kept sending W: lines (empty scale +
        # stepping-on wobble). Throw those away so only readings from NOW count.
        link.drain()
        time.sleep(STEP_ON_SETTLE_SEC)
    link.drain()
    R.info(f"Hold still - weighing for {WEIGHT_SECONDS}s")
    vals = link.weights(WEIGHT_SECONDS, R)

    if not vals:
        return {"weight_kg": None, "steadiness_kg": None, "readings": 0, "zero": zero,
                "error": "no readings from the ESP32"}
    steady = vals[len(vals) // 2:]          # second half = after you've settled
    weight = median(steady)
    if weight < PERSON_MIN_KG:
        return {"weight_kg": weight, "steadiness_kg": None, "readings": len(steady), "zero": zero,
                "error": "nobody on the platform - stand in the middle of it and try again"}
    return {
        "weight_kg": weight,
        "steadiness_kg": (max(steady) - min(steady)) / 2,
        "readings": len(steady),
        "zero": zero,
        "error": None,
    }


# =====================================================================
# 4) TEMPERATURE - MLX90614  -> last reading
# =====================================================================
def _wrist_present(obj_raw, amb):
    """Is a wrist in front of the sensor? (raw object temperature, before the offset)"""
    if obj_raw is None or amb is None:
        return False
    diff = obj_raw - amb
    return diff >= TEMP_DETECT_DIFF_C or (obj_raw >= TEMP_HOT_ROOM_OBJ_C and diff >= TEMP_HOT_ROOM_DIFF_C)


def measure_temp(confirm=None, progress=None):
    """Body temperature from the inside of the wrist (MLX90614).

    Safety (Nov 2026):
      - self-check first: the sensor must answer and its own chip temperature
        must be possible (TEMP_CHIP_RANGE_C), else MeasureError (not the patient)
      - the reading starts by itself once a WRIST is detected (object clearly
        warmer than the room, with a hot-room fallback); the button is a backup
      - "Hold your wrist still" / "move closer" are live warnings only
      - steady part still swinging more than TEMP_WOBBLE_MAX_C -> restarts by
        itself (at most TEMP_MAX_RESTARTS times)
      - result = average of the steady readings; refused below TEMP_MIN_BODY_C
        or above TEMP_MAX_BODY_C, and when no wrist was ever detected
    Returns {"object_c", "ambient_c", "raw_c", "raw_samples", "diff_c",
             "restarts", "wrist_wait_s", "error"}.
    """
    confirm = confirm or _no_confirm
    R = Reporter("temp", progress)
    smbus = need("smbus", "Run on the Pi: sudo apt install python3-smbus")
    bus = smbus.SMBus(I2C_BUS)

    def read_c(reg):
        for _ in range(5):                         # retries for clock-stretching errors
            try:
                raw = bus.read_word_data(MLX90614_ADDR, reg)
            except OSError:
                time.sleep(0.05)
                continue
            if raw & 0x8000:                       # error flag
                return None
            return raw * 0.02 - 273.15
        return None

    info = {"restarts": 0, "wrist_wait_s": None}
    t_prompt = time.time()
    amb = None
    try:
        # 1) self-check before the patient is asked to do anything
        amb = read_c(0x06)
        lo, hi = TEMP_CHIP_RANGE_C
        if amb is None or not lo <= amb <= hi:
            raise MeasureError("temperature sensor gives no sensible reading (chip temperature "
                               + ("missing" if amb is None else f"{amb:.1f} C") + ") - check the MLX90614 wiring")

        step, message = "forehead", "Hold the inside of your wrist 2-5 cm from the sensor"
        while True:
            # 2) wait for a wrist (starts by itself; the screen's button is a backup)
            since = {"t": None, "sent": 0.0}

            def wrist_ready():
                o, a = read_c(0x07), read_c(0x06)
                now = time.time()
                present = _wrist_present(o, a)
                since["t"] = (since["t"] or now) if present else None
                if now - since["sent"] >= 0.3:
                    since["sent"] = now
                    R._send("watching", live={"wrist": present,
                                              "object_c": round(o + TEMP_OFFSET_C, 1) if o is not None else None})
                time.sleep(0.15)
                return present and now - since["t"] >= TEMP_DETECT_STEADY_SEC

            _confirm_auto(confirm, step, message, wrist_ready)
            if info["wrist_wait_s"] is None:
                info["wrist_wait_s"] = round(time.time() - t_prompt, 1)

            # 3) the 5 s reading
            R.info(f"Hold still - reading for {TEMP_SECONDS}s")
            vals, raws, recent, diffs = [], [], [], []
            seen_wrist = False
            t_end = time.time() + TEMP_SECONDS
            while time.time() < t_end:
                o, a = read_c(0x07), read_c(0x06)
                if a is not None:
                    amb = a
                present = _wrist_present(o, amb)
                if o is not None and present:
                    seen_wrist = True
                    raws.append(o)
                    vals.append(o + TEMP_OFFSET_C)
                    diffs.append(o - amb)
                    recent = (recent + [o])[-4:]          # ~1 s of readings
                moving = present and len(recent) >= 3 and max(recent) - min(recent) > TEMP_WOBBLE_WARN_C
                R.tick(t_end, f"now: {(o or 0) + TEMP_OFFSET_C:5.2f} C",
                       object_c=round(o + TEMP_OFFSET_C, 1) if o is not None else None,
                       wrist=present, moving=moving)
                time.sleep(0.25)
            R.end()

            steady = vals[len(vals) // 2:]                # second half = settled
            if seen_wrist and len(steady) >= 2 and max(steady) - min(steady) > TEMP_WOBBLE_MAX_C:
                if info["restarts"] >= TEMP_MAX_RESTARTS:
                    return _temp_result(None, amb, raws, diffs, info,
                                        "no steady reading - the wrist kept moving")
                info["restarts"] += 1
                R._send("restart", "the readings were too unsteady - starting again",
                        restart=info["restarts"])
                step, message = "temp_again", "Hold your wrist still - the reading starts again by itself"
                continue
            break
    finally:
        bus.close()

    # 4) the result: average of the steady readings, only if a wrist was there and it makes sense
    if not seen_wrist:
        return _temp_result(None, amb, raws, diffs, info,
                            "no wrist detected - hold the inside of your wrist 2-5 cm from the sensor")
    body = trimmed_mean(steady) if len(steady) >= 3 else sum(steady) / len(steady)
    if body < TEMP_MIN_BODY_C:
        return _temp_result(body, amb, raws, diffs, info,
                            f"too low ({body:.1f} C) - the wrist was too far away or cold", keep=False)
    if body > TEMP_MAX_BODY_C:
        return _temp_result(body, amb, raws, diffs, info,
                            f"too high ({body:.1f} C) - something warm was in front of the sensor", keep=False)
    return _temp_result(body, amb, raws, diffs, info, None)


def _temp_result(body, amb, raws, diffs, info, error, keep=True):
    """The dict measure_temp returns (+ the numbers for tuning in the field)."""
    return {
        "object_c": body if (keep and error is None) else None,
        "rejected_c": None if (keep and error is None) else body,   # what it would have been
        "ambient_c": amb,
        "raw_c": trimmed_mean(raws) if raws else None,   # avg sensor reading WITHOUT the offset
        "raw_samples": len(raws),
        "diff_c": round(sum(diffs) / len(diffs), 2) if diffs else None,   # wrist minus room
        **info,
        "error": error,
    }

# =====================================================================
# 5) BLOOD PRESSURE - BP monitor + webcam  -> final SYS/DIA/PUL
# =====================================================================
def resolve_cam(cam):
    """Turn the BP cam setting into a camera number for OpenCV.

    On the Pi: follow the by-path link (-> /dev/video2 -> 2), so it is always
    the BP cam on hub port 1.1 (top blue), even if the numbers change after a reboot.
    On Windows: the Pi path doesn't exist there, so use camera 0.
    A plain number (e.g. --cam 2) is used as-is.
    """
    cam = str(cam)
    if cam.isdigit():
        return int(cam)
    if not ON_PI:
        return 0
    real = os.path.realpath(cam)
    if not os.path.exists(real) or not real.startswith("/dev/video"):
        raise MeasureError(f"BP cam not found at {cam} - is it plugged into hub port 1.1 (top blue)? "
                           "(check: ls -l /dev/v4l/by-path/)")
    return int(real[len("/dev/video"):])


def find_esp32_port():
    try:
        from serial.tools import list_ports
    except ImportError:
        return None
    ports = [p.device for p in list_ports.comports() if p.vid in ESP32_USB_IDS]
    return ports[0] if len(ports) == 1 else None


def load_bp():
    """Import bp_auto (it loads bp_cam_settings.json from hardware/, so cd there first)."""
    need("cv2", "pip install opencv-python  (Pi: sudo apt install python3-opencv)")
    old_cwd = os.getcwd()
    os.chdir(HW_DIR)
    try:
        import bp_auto
    finally:
        os.chdir(old_cwd)
    return bp_auto


def make_trigger(bp, kind, port=None, report=None):
    """How the START button gets pressed: 'auto' | 'esp32' | 'pi' | 'manual'."""
    R = report or Reporter("bp")
    if kind == "auto":                     # Pi = GPIO (pi_pins.py), Windows = ESP32
        if ON_PI and not port:
            kind = "pi"
        else:
            port = port or find_esp32_port()
            kind = "esp32" if port else "manual"
    try:
        if kind == "esp32":
            port = port or find_esp32_port()
            if not port:
                raise MeasureError("no ESP32 found - give the port with --bp-port COM5 or /dev/ttyUSB0")
            R.info(f"Trigger: ESP32 on {port}")
            return bp.Esp32Trigger(port, BP_PRESS_MS)
        if kind == "pi":
            R.info(f"Trigger: Pi GPIO{BP_PI_PIN} (pinctrl), lights on GPIO26 relay")
            return bp.PiTrigger(BP_PI_PIN, BP_PRESS_MS / 1000)
    except SystemExit as e:                # bp_auto exits on a missing/silent ESP32
        raise MeasureError(str(e))
    R.info("Trigger: manual (you press START)")
    return bp.ManualTrigger()


def measure_bp(camera=None, trigger=None, bp_port=None, confirm=None, progress=None):
    """One automatic BP measurement (~40-70 s).

    camera   BP webcam: a number or a /dev/v4l/by-path/... path
             (default: BP_CAMERA on the Pi, camera 0 elsewhere)
    trigger  'auto' | 'esp32' | 'pi' | 'manual' (default: BP_TRIGGER on the Pi,
             'auto' elsewhere)
    Returns {"sys", "dia", "pul", "duration_s", "error_code", "error"}.
    (bp_auto.measure() still prints its own [info] lines for now.)
    """
    confirm = confirm or _no_confirm
    R = Reporter("bp", progress)
    if camera is None:
        camera = BP_CAMERA if ON_PI else "0"
    if trigger is None:
        trigger = BP_TRIGGER if ON_PI else "auto"

    bp = load_bp()
    cam = bp.cam
    if not all(cam.settings["rois"].get(f) for f in cam.FIELDS):
        raise MeasureError("SYS/DIA/PUL boxes missing - run hardware/bp_camTEST.py and press w")

    cam_index = resolve_cam(camera)            # fail early if the BP cam is unplugged
    R.info(f"BP cam: /dev/video{cam_index}" if ON_PI else f"BP cam: camera {cam_index}")

    trig = make_trigger(bp, trigger, bp_port, R)
    cap = None
    try:
        confirm("cuff_on", "Put the cuff on (arm at heart level)")
        trig.led(True)                             # lights first, then camera
        time.sleep(getattr(cam, "LIGHT_WARMUP_SEC", 2.0))
        cap = cam.open_camera(cam_index, cam.FRAME_W, cam.FRAME_H)
        cam.apply_exposure(cap, cam.settings["exposure"])
        if hasattr(cam, "warm_up"):
            cam.warm_up(cap)
        R.info("Measuring - keep your arm still and don't talk")
        timeout = getattr(bp, "TIMEOUT", 100)

        def on_tick(elapsed, phase):
            # count UP (the BP monitor decides when it's done); same seconds as the log
            R._send("measuring", f"{phase} {elapsed:5.1f}s", elapsed_s=elapsed,
                    timeout_s=timeout, bp_phase=phase)

        result = bp.measure(cap, trig, window=False, on_tick=on_tick)   # no preview window
    finally:
        if cap is not None:
            cap.release()
        try:
            trig.led(False)
        except (RuntimeError, OSError) as e:
            R.warn(f"could not turn the lights off: {e}")
        trig.close()

    empty = {"sys": None, "dia": None, "pul": None, "duration_s": None, "error_code": None}
    if not result:
        return {**empty, "error": "no result (monitor did not respond / timed out)"}
    if "error" in result:
        return {**empty, "error_code": result["error"],
                "error": f"ERROR {result['error']} - {result['message']}"}
    return {
        "sys": result["sys"],
        "dia": result["dia"],
        "pul": result["pul"],
        "duration_s": result["duration_s"],
        "error_code": None,
        "error": None,
    }
