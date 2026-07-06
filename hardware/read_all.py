#!/usr/bin/env python3
import time
import board, busio
import adafruit_vl53l0x
import RPi.GPIO as GPIO
from hx711 import HX711  # type: ignore
import hardware.max30102 as max30102, hardware.hrcalc as hrcalc

# ---- tuning constants ----
SCALE = 1.0            # HX711 raw units per gram — set after calibrating
SENSOR_MOUNT_MM = None # sensor height above floor in mm; set this to get patient height


def measure_height(duration=10):
    """Stage 1 — VL53L0X. Average the distance over `duration` seconds."""
    i2c = busio.I2C(board.SCL, board.SDA)
    tof = adafruit_vl53l0x.VL53L0X(i2c)
    tof.measurement_timing_budget = 200000

    print(f"  Stand still under the sensor — measuring for {duration}s...")
    samples = []
    t_end = time.time() + duration
    while time.time() < t_end:
        d = tof.range
        if 0 < d < 4000:            # drop obvious out-of-range junk
            samples.append(d)
        time.sleep(0.1)

    i2c.deinit()                    # release the bus for the next sensor
    return sum(samples) / len(samples) if samples else None


def measure_weight(duration=10):
    """Stage 2 — HX711. Tare, then average weight over `duration` seconds."""
    hx = HX711(dout_pin=5, pd_sck_pin=6)
    hx.reset()

    input("  Keep the scale EMPTY, then press Enter to tare...")
    tare = hx.get_raw_data(times=15)
    if not tare:
        print("  HX711 returned nothing — check power and DT/SCK wiring.")
        return None
    offset = sum(tare) / len(tare)

    input("  Step ONTO the scale, then press Enter to weigh...")
    print(f"  Hold still — measuring for {duration}s...")
    samples = []
    t_end = time.time() + duration
    while time.time() < t_end:
        raw = hx.get_raw_data(times=5)
        if raw:
            samples.append(sum(raw) / len(raw))

    if not samples:
        return None
    avg = sum(samples) / len(samples)
    return (avg - offset) / SCALE


def measure_vitals(duration=20):
    """Stage 3 — MAX30102. Return only the average BPM and average SpO2."""
    mx = max30102.MAX30102()

    print(f"  Place your finger on the sensor and hold still — reading for {duration}s...")
    hrs, spo2s = [], []
    t_end = time.time() + duration
    while time.time() < t_end:
        red, ir = mx.read_sequential()
        hr, hr_ok, spo2, spo2_ok = hrcalc.calc_hr_and_spo2(ir, red)
        if hr_ok and 30 < hr < 220:        # keep only plausible values
            hrs.append(hr)
        if spo2_ok and 70 <= spo2 <= 100:
            spo2s.append(spo2)

    avg_hr = sum(hrs) / len(hrs) if hrs else None
    avg_spo2 = sum(spo2s) / len(spo2s) if spo2s else None
    return avg_hr, avg_spo2


def main():
    try:
        while True:
            print("\n=== New measurement ===")
            input("Press Enter to begin...")

            print("\n[1/3] Height")
            dist = measure_height()

            print("\n[2/3] Weight")
            weight = measure_weight()

            print("\n[3/3] Heart rate & SpO2")
            avg_hr, avg_spo2 = measure_vitals()

            print("\n--- Results ---")
            if dist is None:
                print("Height: read failed")
            elif SENSOR_MOUNT_MM:
                print(f"Height: {(SENSOR_MOUNT_MM - dist) / 10:.1f} cm")
            else:
                print(f"Head distance: {dist:.0f} mm  (set SENSOR_MOUNT_MM for height)")
            print(f"Weight: {weight:.1f} g" if weight is not None else "Weight: read failed")
            print(f"Avg heart rate: {avg_hr:.0f} bpm" if avg_hr else "Avg heart rate: no valid reading")
            print(f"Avg SpO2: {avg_spo2:.0f} %" if avg_spo2 else "Avg SpO2: no valid reading")

            if input("\nMeasure another? (y/n): ").strip().lower() != "y":
                break
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        GPIO.cleanup()
        print("Stopped.")


if __name__ == "__main__":
    main()