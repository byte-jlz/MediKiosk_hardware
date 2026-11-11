#!/usr/bin/env python3
"""
sensor_test.py - MediKiosk all-sensor test (one script, no live view)

The measuring code itself lives in hardware/measurements.py (shared with the
MQTT kiosk agent). This file is only the terminal menu: it asks you to press
Enter, prints the countdowns, and formats the results.
TUNING SETTINGS (HR_SECONDS, SENSOR_MOUNT_MM, BP_CAMERA, ...) are at the top
of hardware/measurements.py now.

Tests the 5 kiosk sensors and prints ONE summary value per measurement:
    1) Heart rate + SpO2  (GY-MAX30102)        -> AVERAGE bpm and AVERAGE SpO2
    2) Height             (VL53L0X V2 ToF)     -> AVERAGE distance -> height
    3) Weight             (ESP32 + HX711 + 4x 50kg load cells, over USB)
                                               -> MEDIAN steady reading
    4) Temperature        (GY-906 MLX90614ESF) -> LAST reading
    5) Blood pressure     (BP monitor + USB webcam + START button trigger)
                                               -> final SYS / DIA / PUL
    6) Respiratory rate   (same GY-MAX30102, no extra hardware)
                                               -> breaths/min over 30 s (RR_SECONDS),
                          plus a CSV + plot in sensor_test/rr_logs/
    7) RR + SpO2 + HR     (ONE 30 s finger reading = the kiosk's step 2)
                                               -> compare with 1) and the BP PUL

Menu:
    [1] Test ONE specific sensor (you pick which)
    [2] Test ALL one by one, in the kiosk order
        (height -> weight -> RR+SpO2+HR -> BP -> temp),
        with a summary table at the end
    [3] Calibrate the load cell (tare -> known weight -> live check -> save)
    [4] Calibrate the height sensor (flat board at 2-4 known heights
        -> SENSOR_MOUNT_MM + TOF_SCALE)
    [5] Calibrate the temperature sensor (clinical thermometer vs kiosk
        -> TEMP_OFFSET_C)
    [6] Calibrate ALL, one by one: temperature -> height -> load cell
        (HR / SpO2 / RR have no calibration for now)
    Height + temperature results are saved in hardware/calibration.json,
    which measurements.py loads on every start (sensor_test AND the agent).
    The load cell factor is saved in the ESP32 itself, as before.

=============================================================================
 WIRING  (Raspberry Pi 4B - power EVERYTHING from 3.3V, never 5V, so the
          I2C bus stays at 3.3V and the Pi's GPIO pins are safe)
=============================================================================

 Pi 4B pins used (physical pin numbers):
     (same map as hardware/pi_pins.py)
     Pin 1  = 3.3V   (I2C sensors)          Pin 17 = 3.3V  (HX711 + relay VCC)
     Pin 2  = 5V     (relay COM, LED supply)
     Pin 3  = GPIO2 / SDA (I2C bus 1)       Pin 5  = GPIO3 / SCL (I2C bus 1)
     Pin 6, 9, 14, 20, 25, 30, 34, 39 = GND
     Pin 13 = GPIO27 (BP START button)      Pin 37 = GPIO26 (light relay IN)
     Pin 29 = GPIO5  (HX711 DT)             Pin 31 = GPIO6 (HX711 SCK)

 --- SHARED I2C BUS (the 3 I2C sensors are wired in parallel) -------------
     Each sensor:  VIN -> 3.3V (pin 1)    GND -> GND (pin 6)
                   SDA -> GPIO2 (pin 3)   SCL -> GPIO3 (pin 5)
     Check with:   i2cdetect -y 1   ->  should show 29, 57 and 5a

 1) GY-MAX30102 pulse oximeter  (I2C address 0x57)
     VIN -> 3.3V   GND -> GND   SDA -> GPIO2 (pin 3)   SCL -> GPIO3 (pin 5)
     INT -> not connected
     (6) Respiratory rate uses this same sensor - no extra wiring)

 2) VL53L0X V2 ToF distance  (I2C address 0x29)  - mounted on the overhead
    arm, pointing straight DOWN at the top of the user's head
     VIN -> 3.3V   GND -> GND   SDA -> GPIO2 (pin 3)   SCL -> GPIO3 (pin 5)
     XSHUT, GPIO1 -> not connected

 4) GY-906 MLX90614ESF IR thermometer  (I2C address 0x5A)
     VIN -> 3.3V   GND -> GND   SDA -> GPIO2 (pin 3)   SCL -> GPIO3 (pin 5)

 3) Weight: ESP32 + HX711 + four 50kg half-bridge load cells
    The ESP32 is the ONLY reader of the HX711 (the Pi's Python timing gave
    garbage HX711 reads; the ESP32 read 5 kg to within ~2 g on the same wires).
    Upload hardware/scale_esp32/scale_esp32.ino to it.
     HX711 VCC -> ESP32 3.3V         HX711 GND -> ESP32 GND
     HX711 DT  -> ESP32 GPIO4        HX711 SCK -> ESP32 GPIO5
     ESP32 USB -> Pi USB  (power + data). On the Pi it is found at SCALE_PORT
     (/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0 = CH340 chip), on
     Windows it is auto-detected or given with --scale-port.
     Pi pins 29/31 (GPIO5/6) are no longer used for the scale.
     Load cells -> HX711 E+ / E- / A+ / A-  (keep your current, working
     bridge wiring). Usual layout for 4 cells in the corners:
         top-left  red -> E+      bottom-right red -> E-
         top-right red -> A+      bottom-left  red -> A-
         white/black wires joined to the neighbouring cell's same colour
     (wire colours differ between sellers - the RED is the middle wire)
     Calibration: menu option 3 here. The factor is stored in the ESP32's
     flash (raw units per gram; 19.17 on 2026-10-27).

 5) Blood pressure monitor
     USB webcam  -> hub port 1.1 = TOP BLUE port, facing the LCD (BP_CAMERA
                    by-path setting). The QR cam is the same model and sits
                    on hub port 1.2 = bottom blue -
                    keep each camera in its own port or they swap!
                    On Windows the BP cam is camera 0.
     Camera boxes/tuning come from ../hardware/bp_cam_settings.json
     (make them with hardware/bp_camTEST.py, press w to save)

     START button + camera lights - choose ONE:
     a) Pi directly (DEFAULT on the Pi - replaces the ESP32, see pi_pins.py)
            GPIO27 (pin 13)    -> BP START button signal leg
            Pi GND (pin 14)    -> BP monitor GND (battery -)   <- REQUIRED
            Relay VCC -> 3.3V (pin 17)   Relay GND -> GND (pin 39)
            Relay IN  -> GPIO26 (pin 37) (low-level trigger, RELAY_ACTIVE_LOW)
            5V (pin 2) -> relay COM,     relay NO -> LED +,   LED - -> GND
     b) ESP32 (bp_trigger_esp32.ino - default on the Windows laptop)
            ESP32 USB          -> laptop / Pi USB (auto-detected)
            ESP32 button GPIO  -> BP START button signal leg (pin in sketch)
            ESP32 GND          -> BP monitor GND (battery -)   <- REQUIRED
            Relay (lights): VCC -> ESP32 3.3V, GND -> GND, IN -> ESP32 GPIO
     c) Manual - you press START yourself when told to

=============================================================================
 HOW TO RUN  (on the Pi, inside your venv, from anywhere)
=============================================================================
     python3 sensor_test/sensor_test.py                 # menu
     python3 sensor_test/sensor_test.py --sensor all    # skip the menu
     python3 sensor_test/sensor_test.py --sensor rr     # hr|height|weight|temp|bp|rr|finger
     python3 sensor_test/sensor_test.py --calibrate     # recalibrate the load cell
     python3 sensor_test/sensor_test.py --calibrate-height   # calibrate the ToF height
     python3 sensor_test/sensor_test.py --calibrate-temp     # calibrate the temperature
     python3 sensor_test/sensor_test.py --calibrate-all      # temp -> height -> load cell
     (STOP the kiosk agent first, and restart it after, so it loads the new values)
     python3 sensor_test/sensor_test.py --sensor weight --scale-port /dev/ttyUSB0
     (the RR plot needs matplotlib: sudo apt install python3-matplotlib -
      without it you still get the result and the CSV)
     python3 sensor_test/sensor_test.py --bp-trigger esp32 --bp-port /dev/ttyUSB0
     python3 sensor_test/sensor_test.py --sensor bp --cam 2    # force a camera number

 The BP and weight tests also run on the Windows laptop (I2C tests need the Pi):
     python sensor_test\\sensor_test.py --sensor bp --bp-trigger esp32 --bp-port COM5
     python sensor_test\\sensor_test.py --sensor weight --scale-port COM5
"""

import argparse
import os
import sys

# ---------------- Paths: reuse the code in ../hardware ----------------
HERE = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(HERE)
HW_DIR = os.path.join(REPO_DIR, "hardware")
sys.path.insert(0, HW_DIR)

import measurements as m          # noqa: E402  (needs HW_DIR on the path first)
from measurements import ON_PI    # noqa: E402

ALL_ORDER = ["height", "weight", "finger", "bp", "temp"]   # order for "test all" (= kiosk flow)
MENU_ORDER = ["hr", "height", "weight", "temp", "bp", "rr", "finger"]

NAMES = {
    "hr": "Heart rate & SpO2 (MAX30102)",
    "rr": "Respiratory rate (MAX30102 signal)",
    "finger": "RR + SpO2 + heart rate (ONE 30 s finger reading)",
    "height": "Height (VL53L0X ToF)",
    "weight": "Weight (ESP32 + HX711 + load cells)",
    "temp": "Temperature (MLX90614)",
    "bp": "Blood pressure (BP monitor + webcam)",
    "cal": "Calibrate the load cell (ESP32 scale)",
    "calh": "Calibrate the height sensor (VL53L0X)",
    "calt": "Calibrate the temperature sensor (MLX90614)",
}
CAL_ALL_ORDER = ["calt", "calh", "cal"]   # "calibrate all": temp -> height -> load cell


# =====================================================================
# Terminal versions of the two callbacks measurements.py asks for
# =====================================================================
# A few steps keep their old, terminal-specific wording.
CLI_PROMPTS = {
    "scale_empty": "Keep the scale EMPTY, then press Enter to tare...",
    "finger_rest": "Rest your fingertip on the sensor, sit still, breathe normally,\n"
                   "  then press Enter...",
    "cuff_on": "Put the cuff on (arm at heart level), then press Enter to start...",
}


def cli_confirm(step, message):
    """The person has to do something first -> wait for Enter."""
    input("  " + CLI_PROMPTS.get(step, f"{message}, then press Enter..."))


def make_cli_progress():
    """Print progress events: countdowns on one line, info/warnings below."""
    state = {"counting": False}

    def progress(ev):
        phase = ev.get("phase")
        if phase == "measuring":
            print(f"\r  {ev['message']}  {ev['seconds_left']:4.1f}s left ", end="", flush=True)
            state["counting"] = True
            return
        if state["counting"]:              # finish the countdown line first
            print()
            state["counting"] = False
        if phase == "countdown_end":
            return
        if phase == "warning":
            print(f"  [warn] {ev['message']}")
        else:
            print(f"  {ev['message']}")

    return progress


CLI_PROGRESS = make_cli_progress()


# =====================================================================
# One test per sensor: run the measurement, turn the numbers into text
# =====================================================================
def spo2_raw_text(r):
    """The sensor's own SpO2 before SPO2_OFFSET - compare it with a medical oximeter to calibrate."""
    raw = r.get("spo2_raw_pct")
    if raw is None:
        return "-"
    return f"{raw:.1f} %  (+ SPO2_OFFSET {m.SPO2_OFFSET:+.1f} = shown value; for calibrating)"


def test_hr():
    r = m.measure_hr_spo2(cli_confirm, CLI_PROGRESS)
    hr, spo2 = r["heart_rate_bpm"], r["spo2_pct"]
    out = {
        "Avg heart rate": f"{hr:.0f} bpm  ({r['hr_samples']} samples)" if hr is not None else "no valid reading",
        "Avg SpO2": f"{spo2:.0f} %  ({r['spo2_samples']} samples)" if spo2 is not None else "no valid reading",
        "SpO2 sensor raw": spo2_raw_text(r),
        "Sensor samples": r["sensor_report"],
    }
    if r["ir_level"] is not None:
        out["Last IR level"] = f"{r['ir_level']:.0f}  (finger on = above {m.FINGER_IR_MIN})"
    return out


def test_rr():
    r = m.measure_rr(cli_confirm, CLI_PROGRESS)
    if r.get("peaks_count") is None:               # the recording itself failed
        return {"Respiratory rate": r["error"]}

    a, b = r["rr_peaks"], r["rr_fft"]
    if r["level"] == "CONFIRMED":
        confidence = "HIGH - both methods agree and all checks passed"
    elif r["level"] == "ESTIMATE":
        confidence = f"LOW - both methods agree, but {r['reasons'][0]}. Retry to confirm"
    else:
        confidence = ("NONE - " + "; ".join(r["reasons"]) +
                      ". Retry: rest your finger 5 s first, sit still, breathe normally")

    out = {
        "Respiratory rate": m.rr_summary(r),
        "Confidence": confidence,
        "Breath peaks counted": f"{r['peaks_count']} in {r['duration_s']:.0f}s"
                                + (f" ({r['merged']} false double-peaks merged)" if r["merged"] else "")
                                + (f" -> {a:.1f}/min" if a else ""),
        "FFT estimate": f"{b:.1f}/min" if b else "none",
        "Checks": f"strength {r['strength']:.1f} (>= {r['need_strength']}), "
                  + (f"unevenness {r['steadiness']:.2f} (<= {m.RR_MAX_UNEVEN}), "
                     if r["peaks_count"] >= 3 else "unevenness n/a (too few breaths), ")
                  + f"biggest jump {r['jump'] * 100:.1f}% (<= {m.RR_MAX_JUMP * 100:.0f}%)",
    }
    if r["log_path"]:
        out["Saved"] = os.path.relpath(r["log_path"], REPO_DIR)
    return out


def rr_details(r):
    """Both RR methods + the checks, shown on EVERY result (CONFIRMED, ESTIMATE or none)
    so you can compare them with your own breath count."""
    a, b = r["rr_peaks"], r["rr_fft"]
    return {
        "  Peak count": f"{a:.1f}/min" if a else f"none (only {r['peaks_count']} breath(s) found)",
        "  FFT": f"{b:.1f}/min" if b else "none",
        "  Difference": f"{abs(a - b):.1f}/min (must be <= {m.RR_AGREE})" if a and b else "n/a",
        "  Breaths found": f"{r['peaks_count']} in {r['duration_s']:.0f}s"
                           + (f" ({r['merged']} double-peaks merged)" if r["merged"] else ""),
        "  Checks": f"strength {r['strength']:.1f} (>= {r['need_strength']}), "
                    + (f"unevenness {r['steadiness']:.2f} (<= {m.RR_MAX_UNEVEN}), "
                       if r["peaks_count"] >= 3 else "unevenness n/a (too few breaths), ")
                    + f"biggest jump {r['jump'] * 100:.1f}% (<= {m.RR_MAX_JUMP * 100:.0f}%)",
    }


def test_finger():
    """The kiosk's combined finger step. Compare its HR/SpO2 with test 1 (hr)
    and its HR with the BP monitor's PUL to check the combined reading."""
    r = m.measure_finger(cli_confirm, CLI_PROGRESS)
    if r.get("peaks_count") is None:               # the recording itself failed
        return {"Result": r["error"]}
    hr, spo2 = r["heart_rate_bpm"], r["spo2_pct"]
    out = {
        "Respiratory rate": m.rr_summary(r) + (f"  ({r['level']})" if r["level"] != "NONE" else
                                               "  - " + "; ".join(r["reasons"])),
        **rr_details(r),
        "Avg heart rate": f"{hr:.0f} bpm  ({r['hr_samples']} of {r['windows']} windows)" if hr is not None else "no valid reading",
        "Avg SpO2": f"{spo2:.0f} %  ({r['spo2_samples']} of {r['windows']} windows)" if spo2 is not None else "no valid reading",
        "SpO2 sensor raw": spo2_raw_text(r),
        "Sensor samples": r["sensor_report"],
    }
    if r["log_path"]:
        out["Saved"] = os.path.relpath(r["log_path"], REPO_DIR)
    return out


def test_height():
    r = m.measure_height(cli_confirm, CLI_PROGRESS)
    if r["distance_mm"] is None:
        return {"Avg distance": r["error"]}
    out = {"Avg distance to head": f"{r['distance_mm']:.0f} mm  ({r['samples']} samples, "
                                   f"spread +/-{r['spread_mm']:.0f} mm)"}
    if r["height_cm"] is not None:
        out["Height"] = f"{r['height_cm']:.1f} cm"
    else:
        out["Height"] = "set SENSOR_MOUNT_MM (or run --calibrate-height)"
    return out


def test_weight(args):
    link = m.open_scale(args.scale_port, CLI_PROGRESS)
    try:
        r = m.measure_weight(link, cli_confirm, CLI_PROGRESS)
    finally:
        link.close()
    if r["weight_kg"] is None:
        return {"Weight": r["error"]}
    return {
        "Weight": f"{r['weight_kg']:.2f} kg",
        "Steadiness": f"+/-{r['steadiness_kg']:.2f} kg over {r['readings']} readings",
    }


def test_temp():
    r = m.measure_temp(cli_confirm, CLI_PROGRESS)
    obj, amb = r["object_c"], r["ambient_c"]
    return {
        "Temperature (last)": f"{obj:.2f} C" if obj is not None else "no valid reading",
        "Sensor raw (avg)": (f"{r['raw_c']:.2f} C + offset {m.TEMP_OFFSET_C:+.2f} C"
                             if r["raw_c"] is not None else "no valid reading"),
        "Ambient (last)": f"{amb:.2f} C" if amb is not None else "no valid reading",
    }


def test_bp(args):
    r = m.measure_bp(camera=args.cam, trigger=args.bp_trigger, bp_port=args.bp_port,
                     confirm=cli_confirm, progress=CLI_PROGRESS)
    if r["sys"] is None:
        return {"BP": r["error"]}
    return {
        "Systolic (SYS)": f"{r['sys']} mmHg",
        "Diastolic (DIA)": f"{r['dia']} mmHg",
        "Pulse (PUL)": f"{r['pul']} bpm",
        "Took": f"{r['duration_s']} s  (picture + row saved in hardware/bp_results)",
    }


# =====================================================================
# 3b) CALIBRATE THE LOAD CELL  -> stored in the ESP32's flash
#     (terminal-only: it needs typed answers, so it stays here)
# =====================================================================
def calibrate_weight(args):
    link = m.open_scale(args.scale_port, CLI_PROGRESS)
    report = m.Reporter("cal", CLI_PROGRESS)
    try:
        old = link.factor() or link.ready_factor
        print(f"  Current factor: {old:.4f} raw/g" if old else "  Current factor: unknown")

        input("\n  Step 1: make sure the platform is EMPTY, then press Enter...")
        link.tare()
        print("  Tare complete.")

        print("\n  Step 2: put a KNOWN weight in the CENTER of the platform.")
        print("  Heavier is better (5-20 kg, or a person whose weight you know).")
        input("  Press Enter when it's on and still...")
        while True:
            ans = input("  How many KG is it? (e.g. 5 or 72.5): ").strip()
            try:
                known_kg = float(ans)
                if known_kg > 0:
                    break
            except ValueError:
                pass
            print("  Type a number in kg, bigger than 0.")
        print(f"  Letting it settle for {m.CAL_SETTLE_SEC}s...")
        link.weights(m.CAL_SETTLE_SEC, report, "settling")
        link.send(f"C:{known_kg}")
        got = link.wait_for("CAL:", 10)
        if got is None:
            raise RuntimeError("ESP32 did not return a calibration value")
        factor = float(got)
        ratio = abs(factor) / m.CAL_EXPECTED_PER_G
        print(f"\n  New factor = {factor:.4f} raw/g  ({ratio:.2f}x the 2026-10-27 value)")
        if ratio > 1.5 or ratio < 0.67:
            print("  [warn] far from ~19.17 - was the platform empty at Step 1,")
            print("         the weight centered, and the number typed in KG?")

        print("\n  Step 3: live check - add/remove weight, stand on it, etc.")
        print("  Press Ctrl+C when done checking.")
        try:
            for line in link.lines(3600):
                if line.startswith("W:"):
                    print(f"\r  now: {float(line[2:]):8.2f} kg   ", end="", flush=True)
        except KeyboardInterrupt:
            print()

        ans = input("\n  Save this calibration? (y/n): ").strip().lower()
        if ans == "y":
            link.send("S")
            if link.wait_for("OK:SAVED", 3) is None:
                raise RuntimeError("ESP32 did not confirm the save")
            return {"Calibration": "saved in the ESP32 (survives power off)",
                    "New factor": f"{factor:.4f} raw/g",
                    "Old factor": f"{old:.4f} raw/g" if old else "unknown",
                    "Known weight": f"{known_kg} kg"}
        if old:
            link.send(f"F:{old}")              # put the old factor back
            link.wait_for("FACTOR:", 3)
        return {"Calibration": "NOT saved (kept the old one)",
                "Tried factor": f"{factor:.4f} raw/g"}
    finally:
        link.close()


# =====================================================================
# 3c) CALIBRATE THE HEIGHT SENSOR  -> prints values to paste
#     A flat board is held level under the sensor at 2-4 KNOWN heights
#     (tape-measured from the surface people stand on up to the board's top).
#     Fits  known_height = MOUNT - SCALE * raw_distance  (least squares),
#     so the sensor's offset, % error and the mount height are all fixed at once.
# =====================================================================
def _ask_mm(prompt):
    while True:
        ans = input(prompt).strip().lower()
        if ans in ("", "d", "done"):
            return None
        try:
            v = float(ans) * 10                    # typed in cm
            if 0 < v < 2500:
                return v
        except ValueError:
            pass
        print("  Type the height in CM (e.g. 150 or 162.5), or Enter when done.")


def calibrate_height(args):
    print(f"  Current: SENSOR_MOUNT_MM = {m.SENSOR_MOUNT_MM}, TOF_SCALE = {m.TOF_SCALE}")
    print("\n  EASIEST: use PEOPLE of known height (measured against a wall, no shoes).")
    print("  Type their height, they stand straight under the sensor, Enter.")
    print("  1 person = offset only; 2-3 people of different heights is better.")
    print("  Or: a flat, LIGHT-coloured board (at least 40x40 cm), held LEVEL and")
    print("  centred, 150-185 cm above the platform (measured from the platform UP")
    print("  to the board's top). Nothing else under the arm - the beam is a cone.")
    print("  Check each point: height(mm) + raw should be about the same every time.")
    points = []                                    # (raw_mm, known_mm)
    while True:
        n = len(points) + 1
        known = _ask_mm(f"\n  Point {n}: person/board height in cm (Enter = done): ")
        if known is None:
            if len(points) >= 1:
                break
            print("  Need at least one point.")
            continue
        input("  Person/board in place and still? Press Enter to measure...")
        r = m.measure_height(lambda s, msg: None, CLI_PROGRESS)
        if r["raw_mm"] is None:
            print(f"  [warn] {r['error']} - point skipped")
            continue
        print(f"  raw {r['raw_mm']:.1f} mm  (spread +/-{r['spread_mm']:.1f} mm, "
              f"{r['samples']} samples)   height + raw = {known + r['raw_mm']:.0f} mm")
        if points:
            sums = [y + x for x, y in points]
            avg_sum = sum(sums) / len(sums)
            if abs(known + r["raw_mm"] - avg_sum) > 30:
                print(f"  [warn] earlier points add up to ~{avg_sum:.0f} mm - this one is "
                      f"{known + r['raw_mm'] - avg_sum:+.0f} mm off. Something else in the beam?")
        if r["spread_mm"] > 15:
            print("  [warn] very noisy - board tilted, dark, or too far? Consider redoing it.")
        points.append((r["raw_mm"], known))

    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    if len(points) == 1 or max(xs) - min(xs) < 100:
        if len(points) > 1:
            print("\n  [warn] points are less than 10 cm apart - fitting offset only.")
        scale = 1.0
        mount = sum(y + x for x, y in points) / len(points)
    else:
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        slope = (sum((x - mx) * (y - my) for x, y in points)
                 / sum((x - mx) ** 2 for x in xs))    # = -SCALE
        scale = -slope
        mount = my + scale * mx

    print("\n  Point check (known vs what the kiosk would now say):")
    worst = 0.0
    for x, y in points:
        got = mount - scale * x
        worst = max(worst, abs(got - y))
        print(f"    known {y / 10:6.1f} cm   new {got / 10:6.1f} cm   "
              f"old {(m.SENSOR_MOUNT_MM - m.TOF_SCALE * x) / 10 if m.SENSOR_MOUNT_MM else float('nan'):6.1f} cm")
    problems = []
    if not 0.9 <= scale <= 1.1:
        problems.append(f"TOF_SCALE {scale:.3f} is far from 1.0 (a typo in a height, a tilted "
                        "board, or something else in the beam)")
    if worst > 20:
        problems.append(f"a point is off by {worst / 10:.1f} cm after fitting")

    print(f"\n  New: SENSOR_MOUNT_MM = {mount:.0f}   TOF_SCALE = {scale:.4f}")
    old_mount, old_scale = m.SENSOR_MOUNT_MM, m.TOF_SCALE
    result = {"SENSOR_MOUNT_MM": f"{mount:.0f}  (was {old_mount})",
              "TOF_SCALE": f"{scale:.4f}  (was {old_scale})",
              "Points used": str(len(points)),
              "Worst point error": f"{worst / 10:.1f} cm"}
    if problems:
        for p in problems:
            print(f"  [BAD] {p}")
        print("  NOT saving - these points don't agree. Redo it (people of known")
        print("  height are the easiest reference).")
        return {"Calibration": "NOT saved - bad fit, redo", **result}
    if _ask_save():
        m.save_calibration({"SENSOR_MOUNT_MM": round(mount), "TOF_SCALE": round(scale, 4)},
                           note=f"height, {len(points)} points, worst {worst / 10:.1f} cm")
        print("  Then check with a real person of known height: --sensor height")
        return {"Calibration": "SAVED to hardware/calibration.json", **result}
    return {"Calibration": "NOT saved (kept the old values)", **result}


def _ask_save():
    ans = input("\n  Save and use these from now on? (y/n): ").strip().lower()
    if ans == "y":
        print(f"  Saved to {os.path.relpath(m.CALIBRATION_FILE, REPO_DIR)} - measurements.py")
        print("  loads it on every start. Restart the kiosk agent so it picks it up.")
        return True
    return False


# =====================================================================
# 3d) CALIBRATE THE TEMPERATURE  -> TEMP_OFFSET_C
#     The MLX90614 reads forehead SKIN (~2-4 C below body temp). For each
#     round: take the person's temp with a clinical thermometer, then a kiosk
#     reading straight after. offset = average(thermometer - sensor raw).
# =====================================================================
TEMP_KNOWN_RANGE = (34.0, 42.0)


def calibrate_temp(args):
    print(f"  Current offset: TEMP_OFFSET_C = {m.TEMP_OFFSET_C:+.2f} C")
    print("\n  You need: a clinical thermometer (digital oral/underarm, or ear/forehead).")
    print("  Each round: measure the person with the thermometer, type the value,")
    print("  then do a kiosk reading RIGHT AFTER, the same way the kiosk does it")
    print("  (forehead 2-5 cm from the sensor). Do 3+ rounds, ideally 2-3 people,")
    print("  in the room where the kiosk will stand. Rest a few minutes after")
    print("  coming in from outside / the sun.")
    rounds = []                                    # (known_c, raw_c)
    while True:
        n = len(rounds) + 1
        ans = input(f"\n  Round {n}: thermometer reading in C (Enter = done): ").strip()
        if ans == "":
            if rounds:
                break
            print("  Need at least one round.")
            continue
        try:
            known = float(ans)
        except ValueError:
            print("  Type a number in C, e.g. 36.6")
            continue
        if not TEMP_KNOWN_RANGE[0] <= known <= TEMP_KNOWN_RANGE[1]:
            print(f"  {known} C isn't a body temperature - type it in C "
                  f"({TEMP_KNOWN_RANGE[0]:.0f}-{TEMP_KNOWN_RANGE[1]:.0f}).")
            continue
        r = m.measure_temp(cli_confirm, CLI_PROGRESS)
        if r["raw_c"] is None:
            print(f"  [warn] {r['error']} - round skipped")
            continue
        diff = known - r["raw_c"]
        print(f"  sensor raw {r['raw_c']:.2f} C  ({r['raw_samples']} samples)  "
              f"-> needs {diff:+.2f} C")
        rounds.append((known, r["raw_c"]))

    diffs = [k - raw for k, raw in rounds]
    offset = sum(diffs) / len(diffs)
    spread = max(diffs) - min(diffs)
    print("\n  Round check (thermometer vs what the kiosk would say):")
    for (k, raw), d in zip(rounds, diffs):
        print(f"    thermometer {k:5.2f} C   new {raw + offset:5.2f} C   "
              f"old {raw + m.TEMP_OFFSET_C:5.2f} C")
    if len(rounds) == 1:
        print("  [warn] only 1 round - fine for a quick fix, but 3+ is much safer.")
    elif spread > 1.0:
        print(f"  [warn] the rounds disagree by {spread:.1f} C - distance or angle changed,")
        print("         or someone was warm from outside. Consider redoing.")
    if not 0.0 <= offset <= 6.0:
        print(f"  [warn] offset {offset:+.2f} C is unusual (normally +1 to +4 C) - typo?")

    old = m.TEMP_OFFSET_C
    print(f"\n  New: TEMP_OFFSET_C = {offset:+.2f} C  (was {old:+.2f} C)")
    result = {"TEMP_OFFSET_C": f"{offset:+.2f} C  (was {old:+.2f} C)",
              "Rounds used": str(len(rounds)),
              "Rounds spread": f"{spread:.2f} C" if len(rounds) > 1 else "n/a (1 round)"}
    if _ask_save():
        m.save_calibration({"TEMP_OFFSET_C": round(offset, 2)},
                           note=f"temp, {len(rounds)} rounds, spread {spread:.2f} C")
        return {"Calibration": "SAVED to hardware/calibration.json", **result}
    return {"Calibration": "NOT saved (kept the old value)", **result}


# =====================================================================
# 3e) CALIBRATE ALL  -> temperature, height, load cell (each can be skipped)
# =====================================================================
def calibrate_all(args):
    print("\n" + "#" * 60)
    print("  CALIBRATE ALL  (HR / SpO2 / RR: no calibration for now)")
    print("#" * 60)
    print("  Current values:")
    for key, (value, source) in m.calibration_status().items():
        print(f"    {key:<16} = {value}   ({source})")
    results = {}
    for i, key in enumerate(CAL_ALL_ORDER, 1):
        ans = input(f"\n[{i}/{len(CAL_ALL_ORDER)}] Next: {NAMES[key]}\n"
                    "  Enter = start, s = skip, q = stop: ").strip().lower()
        if ans == "q":
            break
        if ans == "s":
            results[key] = {"Result": "skipped"}
            continue
        results[key] = run_one(key, args)

    print("\n" + "#" * 60)
    print("  CALIBRATION SUMMARY")
    print("#" * 60)
    for key in CAL_ALL_ORDER:
        print(f"\n  {NAMES[key]}")
        for k, v in results.get(key, {"Result": "not done"}).items():
            print(f"    {k:<22}: {v}")
    print("\n  Now in use:")
    for key, (value, source) in m.calibration_status().items():
        print(f"    {key:<16} = {value}   ({source})")
    print("  Restart the kiosk agent so it loads the new values.\n")


# =====================================================================
# Runner + menu
# =====================================================================
def run_one(key, args):
    print("\n" + "=" * 60)
    print(f"  {NAMES[key]}")
    print("=" * 60)
    tests = {"hr": test_hr, "rr": test_rr, "finger": test_finger, "height": test_height, "weight": lambda: test_weight(args),
             "temp": test_temp, "bp": lambda: test_bp(args),
             "cal": lambda: calibrate_weight(args), "calh": lambda: calibrate_height(args),
             "calt": lambda: calibrate_temp(args)}
    try:
        result = tests[key]()
    except KeyboardInterrupt:
        print("\n  [skipped with Ctrl+C]")
        result = {"Result": "cancelled"}
    except (RuntimeError, OSError, SystemExit) as e:     # MeasureError is a RuntimeError
        print(f"\n  [FAILED] {e}")
        result = {"Result": f"FAILED - {e}"}
    print("\n  --- Result ---")
    for k, v in result.items():
        print(f"  {k:<22}: {v}")
    return result


def run_all(args):
    results = {}
    for i, key in enumerate(ALL_ORDER, 1):
        ans = input(f"\n[{i}/{len(ALL_ORDER)}] Next: {NAMES[key]}\n"
                    "  Enter = start, s = skip, q = stop testing: ").strip().lower()
        if ans == "q":
            break
        if ans == "s":
            results[key] = {"Result": "skipped"}
            continue
        results[key] = run_one(key, args)

    print("\n" + "#" * 60)
    print("  SUMMARY")
    print("#" * 60)
    for key in ALL_ORDER:
        print(f"\n  {NAMES[key]}")
        for k, v in results.get(key, {"Result": "not tested"}).items():
            print(f"    {k:<22}: {v}")
    print()


def pick_sensor():
    keys = MENU_ORDER
    print("\n  Which sensor?")
    for i, key in enumerate(keys, 1):
        print(f"    {i}) {NAMES[key]}")
    print("    b) back")
    ans = input(f"  Choose 1-{len(keys)}: ").strip().lower()
    if ans.isdigit() and 1 <= int(ans) <= len(keys):
        return keys[int(ans) - 1]
    return None


def menu(args):
    while True:
        print("\n" + "=" * 60)
        print("  MediKiosk sensor test")
        print("=" * 60)
        print("    1) Test ONE specific sensor")
        print("    2) Test ALL sensors one by one")
        print("    3) Calibrate the load cell (weight scale)")
        print("    4) Calibrate the height sensor (ToF)")
        print("    5) Calibrate the temperature sensor")
        print("    6) Calibrate ALL (temperature -> height -> load cell)")
        print("    q) Quit")
        ans = input("  Choose: ").strip().lower()
        if ans == "1":
            key = pick_sensor()
            if key:
                run_one(key, args)
        elif ans == "2":
            run_all(args)
        elif ans == "3":
            run_one("cal", args)
        elif ans == "4":
            run_one("calh", args)
        elif ans == "5":
            run_one("calt", args)
        elif ans == "6":
            calibrate_all(args)
        elif ans in ("q", "quit", "exit"):
            break


def main():
    ap = argparse.ArgumentParser(description="MediKiosk all-sensor test")
    ap.add_argument("--sensor", choices=MENU_ORDER + ["all"],
                    help="skip the menu and run this test")
    ap.add_argument("--calibrate", action="store_true",
                    help="skip the menu and recalibrate the load cell")
    ap.add_argument("--calibrate-height", action="store_true",
                    help="skip the menu and calibrate the ToF height sensor")
    ap.add_argument("--calibrate-temp", action="store_true",
                    help="skip the menu and calibrate the temperature offset")
    ap.add_argument("--calibrate-all", action="store_true",
                    help="skip the menu and calibrate temperature -> height -> load cell")
    ap.add_argument("--bp-trigger", choices=["auto", "esp32", "pi", "manual"],
                    default=m.BP_TRIGGER if ON_PI else "auto",
                    help="how to press the BP START button (Pi: GPIO27 by default, "
                         "Windows: auto = ESP32)")
    ap.add_argument("--bp-port", help="BP trigger ESP32 serial port (only for --bp-trigger esp32), "
                    "e.g. COM5 or /dev/ttyUSB0")
    ap.add_argument("--scale-port", default=m.SCALE_PORT if ON_PI else None,
                    help="scale ESP32 serial port (Pi default: SCALE_PORT, "
                         "Windows: auto-detect), e.g. COM5 or /dev/ttyUSB0")
    ap.add_argument("--cam", default=m.BP_CAMERA if ON_PI else "0",
                    help="BP webcam: a number (e.g. 2) or a /dev/v4l/by-path/... path")
    args = ap.parse_args()

    try:
        if args.calibrate:
            run_one("cal", args)
        elif args.calibrate_height:
            run_one("calh", args)
        elif args.calibrate_temp:
            run_one("calt", args)
        elif args.calibrate_all:
            calibrate_all(args)
        elif args.sensor == "all":
            run_all(args)
        elif args.sensor:
            run_one(args.sensor, args)
        else:
            menu(args)
    except (KeyboardInterrupt, EOFError):
        print("\nStopped.")


if __name__ == "__main__":
    main()
