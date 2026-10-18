#!/usr/bin/env python3
"""
calibrate_scale.py - Pi version of the HX711_ADC "Calibration" sketch.

Same steps as the Arduino example:
  1. tare with the platform empty
  2. put a known weight on it and type how many kg it is
  3. the script works out the calibration factor and saves it to
     scale_calibration.json, which test_scale.py and read_all.py load

Wiring: HX711 VCC -> 3.3V (pin 17), GND -> GND (pin 30),
        DT -> GPIO5 (pin 29), SCK -> GPIO6 (pin 31)
Keep the HX711 on 3.3V - on 5V its DT pin would send 5V into the Pi.

Tip: use something heavy you know the weight of (a 5 kg rice sack, or a
person who just weighed themselves) - a heavier reference gives a more
accurate factor for a 200 kg (4 x 50 kg) platform.
"""

import json
import os
import time

import RPi.GPIO as GPIO
from hx711 import HX711  # type: ignore

try:
    from hardware import pi_pins
except ImportError:
    import pi_pins

HERE = os.path.dirname(os.path.abspath(__file__))
CAL_FILE = os.path.join(HERE, "scale_calibration.json")


def read_avg(hx, samples=30):
    """Average raw reading, ignoring failed reads (the library returns False)."""
    vals = []
    while len(vals) < samples:
        data = hx.get_raw_data(times=5) or []
        vals += [v for v in data if v is not False]
    vals.sort()
    trim = len(vals) // 10                 # drop top/bottom 10% spikes
    vals = vals[trim:len(vals) - trim] or vals
    return sum(vals) / len(vals)


def main():
    hx = HX711(dout_pin=pi_pins.HX711_DT, pd_sck_pin=pi_pins.HX711_SCK)
    hx.reset()
    time.sleep(0.5)

    input("Step 1: make sure the platform is EMPTY, then press Enter...")
    offset = read_avg(hx)
    print(f"  Tare offset = {offset:.0f}")

    input("Step 2: put your known weight on the platform, then press Enter...")
    loaded = read_avg(hx)
    known_kg = float(input("  How many kg is it? ").strip())

    factor = (loaded - offset) / known_kg  # raw units per kg
    if abs(factor) < 1:
        raise SystemExit("Raw value barely changed - check load cell / E+ E- A+ A- wiring.")
    print(f"  Calibration factor = {factor:.2f} raw units per kg")

    with open(CAL_FILE, "w") as f:
        json.dump({"scale_per_kg": factor, "offset": offset,
                   "known_kg": known_kg, "date": time.strftime("%Y-%m-%d %H:%M")}, f, indent=2)
    print(f"  Saved to {CAL_FILE}")

    print("\nStep 3: live check - add/remove weight. Ctrl+C to finish.")
    try:
        while True:
            kg = (read_avg(hx, 10) - offset) / factor
            print(f"  {kg:7.2f} kg")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    try:
        main()
    finally:
        GPIO.cleanup()
