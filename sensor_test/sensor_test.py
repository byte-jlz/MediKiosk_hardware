#!/usr/bin/env python3
"""
sensor_test.py - MediKiosk all-sensor test (one script, no live view)

Tests the 5 kiosk sensors and prints ONE summary value per sensor:
    1) Heart rate + SpO2  (GY-MAX30102)        -> AVERAGE bpm and AVERAGE SpO2
    2) Height             (VL53L0X V2 ToF)     -> AVERAGE distance -> height
    3) Weight             (4x 50kg load cell + HX711) -> LAST reading
    4) Temperature        (GY-906 MLX90614ESF) -> LAST reading
    5) Blood pressure     (BP monitor + USB webcam + START button trigger)
                                               -> final SYS / DIA / PUL

Two ways to test:
    [1] Test ONE specific sensor (you pick which)
    [2] Test ALL sensors one by one (height -> weight -> temp -> HR -> BP),
        with a summary table at the end

=============================================================================
 WIRING  (Raspberry Pi 4B - power EVERYTHING from 3.3V, never 5V, so the
          I2C bus stays at 3.3V and the Pi's GPIO pins are safe)
=============================================================================

 Pi 4B pins used (physical pin numbers):
     Pin 1  = 3.3V   (I2C sensors)          Pin 17 = 3.3V  (HX711)
     Pin 3  = GPIO2 / SDA (I2C bus 1)       Pin 5  = GPIO3 / SCL (I2C bus 1)
     Pin 6, 9, 14, 20, 25 = GND
     Pin 11 = GPIO17 (BP START button, only if the Pi presses it directly)
     Pin 29 = GPIO5  (HX711 DT)             Pin 31 = GPIO6 (HX711 SCK)

 --- SHARED I2C BUS (the 3 I2C sensors are wired in parallel) -------------
     Each sensor:  VIN -> 3.3V (pin 1)    GND -> GND (pin 6)
                   SDA -> GPIO2 (pin 3)   SCL -> GPIO3 (pin 5)
     Check with:   i2cdetect -y 1   ->  should show 29, 57 and 5a

 1) GY-MAX30102 pulse oximeter  (I2C address 0x57)
     VIN -> 3.3V   GND -> GND   SDA -> GPIO2 (pin 3)   SCL -> GPIO3 (pin 5)
     INT -> not connected

 2) VL53L0X V2 ToF distance  (I2C address 0x29)  - mounted on the overhead
    arm, pointing straight DOWN at the top of the user's head
     VIN -> 3.3V   GND -> GND   SDA -> GPIO2 (pin 3)   SCL -> GPIO3 (pin 5)
     XSHUT, GPIO1 -> not connected

 4) GY-906 MLX90614ESF IR thermometer  (I2C address 0x5A)
     VIN -> 3.3V   GND -> GND   SDA -> GPIO2 (pin 3)   SCL -> GPIO3 (pin 5)

 3) HX711 + four 50kg half-bridge load cells  (NOT I2C - own 2 GPIO pins)
     HX711 VCC -> 3.3V (pin 17)      HX711 GND -> GND (pin 9)
     HX711 DT  -> GPIO5 (pin 29)     HX711 SCK -> GPIO6 (pin 31)
     Load cells -> HX711 E+ / E- / A+ / A-  (keep your current, working
     bridge wiring). Usual layout for 4 cells in the corners:
         top-left  red -> E+      bottom-right red -> E-
         top-right red -> A+      bottom-left  red -> A-
         white/black wires joined to the neighbouring cell's same colour
     (wire colours differ between sellers - the RED is the middle wire)

 5) Blood pressure monitor
     USB webcam  -> any USB port (camera 0), facing the LCD
     Camera boxes/tuning come from ../hardware/bp_cam_settings.json
     (make them with hardware/bp_camTEST.py, press w to save)

     START button - choose ONE:
     a) ESP32 (bp_trigger_esp32.ino, recommended - also drives the lights)
            ESP32 USB          -> Pi / laptop USB (auto-detected)
            ESP32 button GPIO  -> BP START button signal leg (pin in sketch)
            ESP32 GND          -> BP monitor GND (battery -)   <- REQUIRED
            Relay (lights): VCC -> ESP32 3.3V, GND -> GND, IN -> ESP32 GPIO
                            LED +5V -> relay COM, relay NO -> LED +
     b) Pi directly (pinctrl, no lights)
            GPIO17 (pin 11)    -> BP START button signal leg
            Pi GND (pin 14)    -> BP monitor GND (battery -)   <- REQUIRED
     c) Manual - you press START yourself when told to

=============================================================================
 HOW TO RUN  (on the Pi, inside your venv, from anywhere)
=============================================================================
     python3 sensor_test/sensor_test.py                 # menu
     python3 sensor_test/sensor_test.py --sensor all    # skip the menu
     python3 sensor_test/sensor_test.py --sensor hr     # hr|height|weight|temp|bp
     python3 sensor_test/sensor_test.py --bp-trigger pi # esp32|pi|manual|auto
     python3 sensor_test/sensor_test.py --bp-port /dev/ttyUSB0

 The BP test also runs on the Windows laptop (the I2C/HX711 tests need the Pi):
     python sensor_test\\sensor_test.py --sensor bp --bp-port COM5
"""

import argparse
import os
import sys
import time

# ---------------- Paths: reuse the drivers in ../hardware ----------------
HERE = os.path.dirname(os.path.abspath(__file__))
HW_DIR = os.path.join(os.path.dirname(HERE), "hardware")
sys.path.insert(0, HW_DIR)

# ---------------- Settings (tune here) ----------------
I2C_BUS = 1

# Heart rate (MAX30102)
HR_SECONDS = 20          # how long to read
HR_RANGE = (40, 200)     # plausible bpm, anything else is dropped
SPO2_RANGE = (70, 100)   # plausible %
FINGER_IR_MIN = 50000    # raw IR below this = no finger on the sensor
MAX30102_ADDR = 0x57
MAX30102_PART_ID = 0x15

# Height (VL53L0X)
HEIGHT_SECONDS = 5
SENSOR_MOUNT_MM = None   # sensor height above the FLOOR in mm (e.g. 2150).
                         # None = only print the distance to the head.
TOF_RANGE_MM = (30, 2500)

# Weight (HX711)
HX711_DT, HX711_SCK = 5, 6   # BCM numbers (pins 29 / 31)
SCALE = 1.0              # raw units per gram - PUT YOUR calibrate_scale.py VALUE HERE
WEIGHT_SECONDS = 5

# Temperature (MLX90614)
MLX90614_ADDR = 0x5A
TEMP_SECONDS = 5
TEMP_OFFSET_C = 0.0      # add to the object reading if you calibrate later

# Blood pressure
BP_CAMERA = 0
BP_PI_PIN = 17
BP_PRESS_MS = 900

TRIM = 0.1               # trimmed average: drop the lowest/highest 10% of samples
ALL_ORDER = ["height", "weight", "temp", "hr", "bp"]   # order for "test all"

NAMES = {
    "hr": "Heart rate & SpO2 (MAX30102)",
    "height": "Height (VL53L0X ToF)",
    "weight": "Weight (HX711 + load cells)",
    "temp": "Temperature (MLX90614)",
    "bp": "Blood pressure (BP monitor + webcam)",
}


# ---------------- Small helpers ----------------
def trimmed_mean(values, trim=TRIM):
    if not values:
        return None
    v = sorted(values)
    k = int(len(v) * trim)
    if len(v) - 2 * k >= 3:
        v = v[k:len(v) - k]
    return sum(v) / len(v)


def countdown_line(t_end, label):
    left = max(0, t_end - time.time())
    print(f"\r  {label}  {left:4.1f}s left ", end="", flush=True)


def need(module_name, hint):
    """Import a hardware library, or explain what's missing."""
    try:
        return __import__(module_name)
    except ImportError as e:
        raise RuntimeError(f"cannot import {module_name} ({e}). {hint}")


# =====================================================================
# 1) HEART RATE + SpO2 - MAX30102  -> average
# =====================================================================
def test_hr():
    smbus = need("smbus", "Run on the Pi: sudo apt install python3-smbus")
    max30102 = need("max30102", f"max30102.py not found in {HW_DIR}")
    hrcalc = need("hrcalc", f"hrcalc.py not found in {HW_DIR}")

    # part-ID check first, so a wiring problem gives a clear message
    bus = smbus.SMBus(I2C_BUS)
    try:
        part = bus.read_byte_data(MAX30102_ADDR, 0xFF)
    except OSError as e:
        raise RuntimeError(f"MAX30102 not answering at 0x57 ({e}) - check wiring / i2cdetect")
    finally:
        bus.close()
    if part != MAX30102_PART_ID:
        print(f"  [warn] part ID is 0x{part:02X}, expected 0x15 (clone board?) - trying anyway")

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
            print(f"  [warn] MAX30102 setup failed ({e}), retrying...")
            time.sleep(0.5)
    if sensor is None:
        raise RuntimeError("MAX30102 setup failed 3 times - check power (3.3V) and SDA/SCL")

    def read_block(n):
        for _ in range(3):
            try:
                return sensor.read_sequential(n)
            except OSError:
                time.sleep(0.05)
        return [], []

    input("  Place your fingertip gently on the sensor, then press Enter...")
    print(f"  Hold still - reading for {HR_SECONDS}s")
    hrs, spo2s = [], []
    no_finger = 0
    try:
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
            countdown_line(t_end, f"valid HR samples: {len(hrs):2d}")
            r, i = read_block(25)                  # slide the window by 1 s
            red, ir = (red + r)[-100:], (ir + i)[-100:]
        print()
    finally:
        try:
            sensor.shutdown()
            sensor.bus.close()
        except OSError:
            pass

    avg_hr, avg_spo2 = trimmed_mean(hrs), trimmed_mean(spo2s)
    if avg_hr is None and no_finger:
        print("  [warn] IR signal was too weak - finger not on the sensor?")
    return {
        "Avg heart rate": f"{avg_hr:.0f} bpm  ({len(hrs)} samples)" if avg_hr else "no valid reading",
        "Avg SpO2": f"{avg_spo2:.0f} %  ({len(spo2s)} samples)" if avg_spo2 else "no valid reading",
    }


# =====================================================================
# 2) HEIGHT - VL53L0X  -> average
# =====================================================================
def test_height():
    board = need("board", "pip install adafruit-blinka (run on the Pi)")
    busio = need("busio", "pip install adafruit-blinka (run on the Pi)")
    vl53 = need("adafruit_vl53l0x", "pip install adafruit-circuitpython-vl53l0x")

    i2c = busio.I2C(board.SCL, board.SDA)
    samples = []
    try:
        tof = vl53.VL53L0X(i2c)
        tof.measurement_timing_budget = 200000     # 200 ms = steadier readings
        input("  Stand straight under the sensor, then press Enter...")
        print(f"  Stand still - measuring for {HEIGHT_SECONDS}s")
        t_end = time.time() + HEIGHT_SECONDS
        while time.time() < t_end:
            try:
                d = tof.range
            except OSError:
                continue
            if TOF_RANGE_MM[0] < d < TOF_RANGE_MM[1]:
                samples.append(d)
            countdown_line(t_end, f"samples: {len(samples):3d}")
        print()
    finally:
        i2c.deinit()                               # free the bus for the next sensor

    avg = trimmed_mean(samples)
    if avg is None:
        return {"Avg distance": "no valid reading (nothing under the sensor?)"}
    out = {"Avg distance to head": f"{avg:.0f} mm  ({len(samples)} samples)"}
    if SENSOR_MOUNT_MM:
        out["Height"] = f"{(SENSOR_MOUNT_MM - avg) / 10:.1f} cm"
    else:
        out["Height"] = "set SENSOR_MOUNT_MM at the top of the script"
    return out


# =====================================================================
# 3) WEIGHT - HX711 + load cells  -> last reading
# =====================================================================
def test_weight():
    hx711 = need("hx711", "pip install hx711 (gandalf15 library)")
    GPIO = need("RPi.GPIO", "run on the Pi").GPIO

    hx = hx711.HX711(dout_pin=HX711_DT, pd_sck_pin=HX711_SCK)
    try:
        hx.reset()
        input("  Keep the scale EMPTY, then press Enter to tare...")
        tare = hx.get_raw_data(times=15)
        if not tare:
            raise RuntimeError("HX711 returned no data - check VCC/GND and DT/SCK wiring")
        offset = sum(tare) / len(tare)

        input("  Step ONTO the scale, then press Enter...")
        print(f"  Hold still - weighing for {WEIGHT_SECONDS}s")
        last = None
        t_end = time.time() + WEIGHT_SECONDS
        while time.time() < t_end:
            raw = hx.get_raw_data(times=5)
            if raw:
                last = (sum(raw) / len(raw) - offset) / SCALE
            countdown_line(t_end, f"now: {last / 1000 if last is not None else 0:6.2f} kg")
        print()
    finally:
        GPIO.cleanup((HX711_DT, HX711_SCK))

    if last is None:
        return {"Weight": "no valid reading"}
    out = {"Weight (last)": f"{last / 1000:.2f} kg  ({last:.0f} g)"}
    if SCALE == 1.0:
        out["Note"] = "SCALE is 1.0 = raw units, put your calibration value in SCALE"
    return out


# =====================================================================
# 4) TEMPERATURE - MLX90614  -> last reading
# =====================================================================
def test_temp():
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
        input("  Hold your forehead/wrist 2-5 cm from the sensor, then press Enter...")
        print(f"  Hold still - reading for {TEMP_SECONDS}s")
        t_end = time.time() + TEMP_SECONDS
        while time.time() < t_end:
            o, a = read_c(0x07), read_c(0x06)      # 0x07 object, 0x06 ambient
            if o is not None:
                obj = o + TEMP_OFFSET_C
            if a is not None:
                amb = a
            countdown_line(t_end, f"now: {obj if obj is not None else 0:5.2f} C")
            time.sleep(0.25)
        print()
    finally:
        bus.close()

    return {
        "Temperature (last)": f"{obj:.2f} C" if obj is not None else "no valid reading",
        "Ambient (last)": f"{amb:.2f} C" if amb is not None else "no valid reading",
    }


# =====================================================================
# 5) BLOOD PRESSURE - BP monitor + webcam  -> final SYS/DIA/PUL
# =====================================================================
ESP32_USB_IDS = {0x10C4, 0x1A86, 0x0403, 0x303A}   # CP210x, CH340, FTDI, Espressif USB


def find_esp32_port():
    try:
        from serial.tools import list_ports
    except ImportError:
        return None
    ports = [p.device for p in list_ports.comports() if p.vid in ESP32_USB_IDS]
    return ports[0] if len(ports) == 1 else None


def make_trigger(bp, args):
    kind = args.bp_trigger
    port = args.bp_port
    if kind == "auto":
        port = port or find_esp32_port()
        if port:
            kind = "esp32"
        elif sys.platform.startswith("linux"):
            kind = "pi"
        else:
            kind = "manual"
    if kind == "esp32":
        port = port or find_esp32_port()
        if not port:
            raise RuntimeError("no ESP32 found - give the port with --bp-port COM5 or /dev/ttyUSB0")
        print(f"  Trigger: ESP32 on {port}")
        return bp.Esp32Trigger(port, BP_PRESS_MS)
    if kind == "pi":
        print(f"  Trigger: Pi GPIO{BP_PI_PIN} (pinctrl)")
        return bp.PiTrigger(BP_PI_PIN, BP_PRESS_MS / 1000)
    print("  Trigger: manual (you press START)")
    return bp.ManualTrigger()


def test_bp(args):
    need("cv2", "pip install opencv-python  (Pi: sudo apt install python3-opencv)")
    old_cwd = os.getcwd()
    os.chdir(HW_DIR)             # bp_camTEST loads bp_cam_settings.json from here
    try:
        import bp_auto as bp
    finally:
        os.chdir(old_cwd)
    cam = bp.cam
    if not all(cam.settings["rois"].get(f) for f in cam.FIELDS):
        raise RuntimeError("SYS/DIA/PUL boxes missing - run hardware/bp_camTEST.py and press w")

    trigger = make_trigger(bp, args)
    cap = None
    try:
        input("  Put the cuff on (arm at heart level), then press Enter to start...")
        trigger.led(True)                          # lights first, then camera
        time.sleep(getattr(cam, "LIGHT_WARMUP_SEC", 2.0))
        cap = cam.open_camera(args.cam, cam.FRAME_W, cam.FRAME_H)
        cam.apply_exposure(cap, cam.settings["exposure"])
        if hasattr(cam, "warm_up"):
            cam.warm_up(cap)
        result = bp.measure(cap, trigger, window=False)   # no preview window
    finally:
        if cap is not None:
            cap.release()
        try:
            trigger.led(False)
        except (RuntimeError, OSError) as e:
            print(f"  [warn] could not turn the lights off: {e}")
        trigger.close()

    if not result:
        return {"BP": "no result (monitor did not respond / timed out)"}
    if "error" in result:
        return {"BP": f"ERROR {result['error']} - {result['message']}"}
    return {
        "Systolic (SYS)": f"{result['sys']} mmHg",
        "Diastolic (DIA)": f"{result['dia']} mmHg",
        "Pulse (PUL)": f"{result['pul']} bpm",
        "Took": f"{result['duration_s']} s  (picture + row saved in hardware/bp_results)",
    }


# =====================================================================
# Runner + menu
# =====================================================================
def run_one(key, args):
    print("\n" + "=" * 60)
    print(f"  {NAMES[key]}")
    print("=" * 60)
    tests = {"hr": test_hr, "height": test_height, "weight": test_weight,
             "temp": test_temp, "bp": lambda: test_bp(args)}
    try:
        result = tests[key]()
    except KeyboardInterrupt:
        print("\n  [skipped with Ctrl+C]")
        result = {"Result": "cancelled"}
    except (RuntimeError, OSError, SystemExit) as e:
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
    keys = ["hr", "height", "weight", "temp", "bp"]
    print("\n  Which sensor?")
    for i, key in enumerate(keys, 1):
        print(f"    {i}) {NAMES[key]}")
    print("    b) back")
    ans = input("  Choose 1-5: ").strip().lower()
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
        print("    q) Quit")
        ans = input("  Choose: ").strip().lower()
        if ans == "1":
            key = pick_sensor()
            if key:
                run_one(key, args)
        elif ans == "2":
            run_all(args)
        elif ans in ("q", "quit", "exit"):
            break


def main():
    ap = argparse.ArgumentParser(description="MediKiosk all-sensor test")
    ap.add_argument("--sensor", choices=["hr", "height", "weight", "temp", "bp", "all"],
                    help="skip the menu and run this test")
    ap.add_argument("--bp-trigger", choices=["auto", "esp32", "pi", "manual"], default="auto",
                    help="how to press the BP START button (auto: ESP32 if found, else Pi GPIO)")
    ap.add_argument("--bp-port", help="ESP32 serial port, e.g. COM5 or /dev/ttyUSB0")
    ap.add_argument("--cam", type=int, default=BP_CAMERA, help="webcam index for the BP test")
    args = ap.parse_args()

    try:
        if args.sensor == "all":
            run_all(args)
        elif args.sensor:
            run_one(args.sensor, args)
        else:
            menu(args)
    except (KeyboardInterrupt, EOFError):
        print("\nStopped.")


if __name__ == "__main__":
    main()
