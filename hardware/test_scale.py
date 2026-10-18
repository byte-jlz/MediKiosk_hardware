import json
import os

import RPi.GPIO as GPIO
from hx711 import HX711  # type: ignore

try:
    from hardware import pi_pins
except ImportError:
    import pi_pins

# Calibration factor comes from calibrate_scale.py (raw units per kg)
CAL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scale_calibration.json")
try:
    SCALE = json.load(open(CAL_FILE))["scale_per_kg"]
except (OSError, KeyError, ValueError):
    SCALE = None
    print("No scale_calibration.json yet - run calibrate_scale.py. Showing raw values only.")

hx = HX711(dout_pin=pi_pins.HX711_DT, pd_sck_pin=pi_pins.HX711_SCK)
hx.reset()


def read_avg(times):
    data = [v for v in (hx.get_raw_data(times=times) or []) if v is not False]
    return sum(data) / len(data) if data else None


# Tare: record baseline with nothing on scale
print("Taring... keep scale empty.")
offset = read_avg(15)
if offset is None:
    raise SystemExit("HX711 returned nothing - check power and DT/SCK wiring.")
print(f"Tared (offset={offset:.0f}). Put weight on now.")

try:
    while True:
        avg = read_avg(5)
        if avg is None:
            continue
        if SCALE:
            print(f"Weight: {(avg - offset) / SCALE:.2f} kg  (raw avg: {avg:.0f})")
        else:
            print(f"Raw change: {avg - offset:.0f}  (raw avg: {avg:.0f})")
except (KeyboardInterrupt, SystemExit):
    GPIO.cleanup()
