#!/usr/bin/env python3
"""
measurements.py - every MediKiosk measurement as a plain function.

The measuring code lives here ONCE, so the terminal test tool
(sensor_test/sensor_test.py) and the MQTT kiosk agent (next step) run
exactly the same code.

    measure_hr_spo2()   heart rate + SpO2      (MAX30102)
    measure_rr()        respiratory rate       (same MAX30102 signal)
    measure_finger()    RR + SpO2 + HR from ONE 60 s finger reading (kiosk flow)
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
SPO2_RANGE = (70, 100)   # plausible %
FINGER_IR_MIN = 50000    # raw IR below this = no finger on the sensor
MAX30102_ADDR = 0x57
MAX30102_PART_ID = 0x15

# Respiratory rate (from the same MAX30102 signal)
RR_SECONDS = 60          # 60 s = ~10-15 breaths at rest (30 s was tried 2026-10-31, kept 60 for accuracy)
RR_BAND_HZ = (0.1, 0.5)  # breathing speeds the filter keeps (= 6-30 breaths/min)
RR_RANGE = (6, 30)       # plausible breaths/min
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
SENSOR_MOUNT_MM = 1994   # sensor height above the FLOOR in mm (e.g. 2150).
                         # None = only report the distance to the head.
TOF_RANGE_MM = (30, 2500)
TOF_TIMING_BUDGET_US = 200000   # 200 ms = steadier readings

# Weight (ESP32 + HX711 - see the WEIGHT section below)
WEIGHT_SECONDS = 5
STEP_ON_SETTLE_SEC = 0.5   # pause after "I'm on the scale" before weighing starts
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
TEMP_OFFSET_C = 2.9      # forehead skin -> body temp: MLX 33.4 vs thermometer 36.3 (2026-10-30, 1 reading)

# Blood pressure
# BP cam = hub port 3 (QR cam = hub port 1, same camera model). The by-path
# name follows the USB PORT, so it stays right after a reboot.
BP_CAMERA = "/dev/v4l/by-path/platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.3:1.0-video-index0"
BP_TRIGGER = "pi"        # auto | esp32 | pi | manual  (Pi presses START on GPIO27, pin 13)
BP_PI_PIN = 27          # was 17 (pin 11) - damaged 2026-10-31
BP_PRESS_MS = 900

ESP32_USB_IDS = {0x10C4, 0x1A86, 0x0403, 0x303A}   # CP210x, CH340, FTDI, Espressif USB

TRIM = 0.1               # trimmed average: drop the lowest/highest 10% of samples


# =====================================================================
# Errors + the two callbacks
# =====================================================================
class MeasureError(RuntimeError):
    """A hardware / setup problem: sensor missing, wiring, library not installed."""


class Cancelled(Exception):
    """Raise this from confirm() or progress() to stop a measurement."""


def _no_confirm(step, message):
    pass                 # nobody to ask -> go straight on


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


# =====================================================================
# Small helpers
# =====================================================================
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
        "spo2_pct": float(avg_spo2) if avg_spo2 is not None else None,
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
    spec = np.abs(np.fft.rfft(wave * np.hanning(n), pad))
    fp = np.fft.rfftfreq(pad, 1 / fs)
    band = (fp >= RR_BAND_HZ[0]) & (fp <= RR_BAND_HZ[1])
    rr_fft = float(fp[band][np.argmax(spec[band])] * 60) if band.any() else None
    power = spec[band] ** 2
    strength = float(power.max() / power.mean()) if band.any() else 0.0   # one clear rhythm?

    # clean up the peaks using the FFT's rhythm - but only if that rhythm is clear
    fft_gap = fs * 60 / rr_fft if rr_fft and strength >= RR_MIN_STRENGTH_AGREE else None
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
RR_EST_UNEVEN = 0.30      # estimate: breaths at most this uneven


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
    elif (res["jump"] <= RR_MAX_JUMP and both and abs(a - b) <= RR_EST_AGREE
          and res["strength"] >= RR_EST_STRENGTH and res["steadiness"] <= RR_EST_UNEVEN):
        rr = (a + b) / 2
        out.update(level="ESTIMATE", rr=rr,
                   low=round(min(a, b, rr - 1)), high=round(max(a, b, rr + 1)))
    return out


def rr_summary(result):
    """Short human text for an RR result (used for the plot title and the CLI)."""
    if result["level"] == "CONFIRMED":
        return f"{result['rr_bpm']:.0f} breaths/min"
    if result["level"] == "ESTIMATE":
        return (f"~{result['rr_bpm']:.0f} breaths/min "
                f"(estimate, likely {result['low']}-{result['high']})")
    return "no reliable reading"


def _record_finger(R, confirm, seconds, step, message):
    """Record `seconds` of fingertip signal, keeping BOTH light channels.

    The MAX30102 always gives red + infrared together (~25 samples/s). Nothing
    is calculated while recording, so the sample rate stays steady; the
    analysis runs afterwards.
    Returns (red, ir, elapsed_s, low_blocks, sensor_report).
    """
    sensor, read_block, flush = open_max30102(R)
    red, ir, low_blocks = [], [], 0
    try:
        confirm(step, message)
        flush()                                    # FIFO filled up while waiting - empty it
        R.info(f"Don't talk or move - reading for {seconds}s")
        read_block(25)                             # drop the first second (settling)
        t0 = time.time()
        t_end = t0 + seconds
        while time.time() < t_end:
            r, i = read_block(25)
            if i and sum(i) / len(i) < FINGER_IR_MIN:
                low_blocks += 1
            red += r
            ir += i
            R.tick(t_end, f"samples: {len(ir):4d}", samples=len(ir))
        elapsed = time.time() - t0
        R.end()
    finally:
        close_max30102(sensor)
    sensor_report = read_block.report()
    R.info(f"Sensor samples: {sensor_report}")
    return red, ir, elapsed, low_blocks, sensor_report


def _recording_problem(ir, low_blocks):
    """Text if the recording itself is unusable, else None."""
    if low_blocks > 2:
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
        "spo2_pct": float(avg_spo2) if avg_spo2 is not None else None,
        "spo2_samples": len(spo2s),
        "windows": windows,
        "no_finger_windows": no_finger,
    }


def measure_rr(confirm=None, progress=None, save_log=True):
    """Respiratory rate from the fingertip signal (RR_SECONDS, default 60 s).

    Returns {"rr_bpm", "level" (CONFIRMED/ESTIMATE/NONE), "low", "high",
             "reasons", ... quality numbers ..., "log_path", "error"}.
    If the recording itself failed (finger left, too few samples) only
    {"rr_bpm": None, "level": "NONE", "sensor_report", "error"} is returned.
    """
    confirm = confirm or _no_confirm
    R = Reporter("rr", progress)
    red, ir, elapsed, low_blocks, report = _record_finger(
        R, confirm, RR_SECONDS, "finger_rest",
        "Rest your fingertip on the sensor, sit still, breathe normally")
    problem = _recording_problem(ir, low_blocks)
    if problem:
        return {"rr_bpm": None, "level": "NONE", "sensor_report": report, "error": problem}
    return _analyse_rr(R, ir, elapsed, report, save_log)


def measure_finger(confirm=None, progress=None, save_log=True):
    """ONE finger reading for three vitals: respiratory rate, SpO2 and heart rate.

    Records RR_SECONDS (60 s) of red + infrared, THEN calculates:
      - respiratory rate from the IR breathing wave (same as measure_rr)
      - heart rate + SpO2 with hrcalc over every 4 s window (more windows than
        the 20 s measure_hr_spo2, so steadier averages)

    Returns everything measure_rr returns, plus "heart_rate_bpm",
    "hr_samples", "spo2_pct", "spo2_samples", and an overall "error"
    (None only when RR, HR and SpO2 all have a reading).
    """
    confirm = confirm or _no_confirm
    R = Reporter("finger", progress)
    red, ir, elapsed, low_blocks, report = _record_finger(
        R, confirm, RR_SECONDS, "finger_rest",
        "Rest your fingertip on the sensor, sit still and breathe normally")
    problem = _recording_problem(ir, low_blocks)
    if problem:
        return {"rr_bpm": None, "level": "NONE", "heart_rate_bpm": None, "spo2_pct": None,
                "hr_samples": 0, "spo2_samples": 0, "sensor_report": report,
                "rr_error": problem, "error": problem}

    result = _analyse_rr(R, ir, elapsed, report, save_log)
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
def measure_height(confirm=None, progress=None, mount_mm=None):
    """Distance from the overhead sensor to the top of the head.

    Returns {"distance_mm", "samples", "height_cm", "error"}.
    height_cm is None while SENSOR_MOUNT_MM (or mount_mm) isn't set.
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
        tof = vl53.VL53L0X(i2c)
        tof.measurement_timing_budget = TOF_TIMING_BUDGET_US
        confirm("stand_under", "Stand straight under the sensor")
        R.info(f"Stand still - measuring for {HEIGHT_SECONDS}s")
        t_end = time.time() + HEIGHT_SECONDS
        while time.time() < t_end:
            try:
                d = tof.range
            except OSError:
                continue
            if TOF_RANGE_MM[0] < d < TOF_RANGE_MM[1]:
                samples.append(d)
            R.tick(t_end, f"samples: {len(samples):3d}", samples=len(samples))
        R.end()
    finally:
        i2c.deinit()                               # free the bus for the next sensor

    avg = trimmed_mean(samples)
    if avg is None:
        return {"distance_mm": None, "samples": 0, "height_cm": None,
                "error": "no valid reading (nothing under the sensor?)"}
    return {
        "distance_mm": float(avg),
        "samples": len(samples),
        "height_cm": (mount_mm - avg) / 10 if mount_mm else None,
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
    """Tare, then weigh. `link` is an open ScaleLink (see open_scale()).

    Returns {"weight_kg", "steadiness_kg", "readings", "error"}.
    """
    confirm = confirm or _no_confirm
    R = Reporter("weight", progress)
    confirm("scale_empty", "Keep the scale EMPTY (it tares next)")
    link.clear_line()          # the agent keeps the port open for hours - clear junk first
    link.tare()
    R.info("Tared.")
    confirm("step_on", "Step ONTO the scale")
    # While the screen waited for "I'm on the scale", the ESP32 kept sending
    # W: lines (empty scale + stepping-on wobble). Throw those away so only
    # readings from NOW on are counted.
    link.drain()
    time.sleep(STEP_ON_SETTLE_SEC)
    link.drain()
    R.info(f"Hold still - weighing for {WEIGHT_SECONDS}s")
    vals = link.weights(WEIGHT_SECONDS, R)

    if not vals:
        return {"weight_kg": None, "steadiness_kg": None, "readings": 0,
                "error": "no readings from the ESP32"}
    steady = vals[len(vals) // 2:]          # second half = after you've settled
    return {
        "weight_kg": median(steady),
        "steadiness_kg": (max(steady) - min(steady)) / 2,
        "readings": len(steady),
        "error": None,
    }


# =====================================================================
# 4) TEMPERATURE - MLX90614  -> last reading
# =====================================================================
def measure_temp(confirm=None, progress=None):
    """Returns {"object_c", "ambient_c", "error"} (last valid readings)."""
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

    obj = amb = None
    try:
        confirm("forehead", "Hold your forehead/wrist 2-5 cm from the sensor")
        R.info(f"Hold still - reading for {TEMP_SECONDS}s")
        t_end = time.time() + TEMP_SECONDS
        while time.time() < t_end:
            o, a = read_c(0x07), read_c(0x06)      # 0x07 object, 0x06 ambient
            if o is not None:
                obj = o + TEMP_OFFSET_C
            if a is not None:
                amb = a
            R.tick(t_end, f"now: {obj if obj is not None else 0:5.2f} C", object_c=obj)
            time.sleep(0.25)
        R.end()
    finally:
        bus.close()

    return {
        "object_c": obj,
        "ambient_c": amb,
        "error": None if obj is not None else "no valid reading",
    }


# =====================================================================
# 5) BLOOD PRESSURE - BP monitor + webcam  -> final SYS/DIA/PUL
# =====================================================================
def resolve_cam(cam):
    """Turn the BP cam setting into a camera number for OpenCV.

    On the Pi: follow the by-path link (-> /dev/video2 -> 2), so it is always
    the BP cam on hub port 3, even if the numbers change after a reboot.
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
        raise MeasureError(f"BP cam not found at {cam} - is it plugged into hub port 3? "
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
        result = bp.measure(cap, trig, window=False)   # no preview window
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
