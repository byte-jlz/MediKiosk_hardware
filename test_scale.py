import RPi.GPIO as GPIO
from hx711 import HX711  # type: ignore

hx = HX711(dout_pin=5, pd_sck_pin=6)
hx.reset()

# Tare: record baseline with nothing on scale
print("Taring... keep scale empty.")
tare_data = hx.get_raw_data(times=10)
offset = sum(tare_data) / len(tare_data)
print(f"Tared (offset={offset:.0f}). Put weight on now.")

# Calibration factor — adjust this after measuring a known weight
SCALE = 1.0  # raw units per gram; tune with a known weight

try:
    while True:
        raw = hx.get_raw_data(times=5)
        avg = sum(raw) / len(raw)
        weight = (avg - offset) / SCALE
        print(f"Weight: {weight:.1f} g  (raw avg: {avg:.0f})")
        hx.power_down()
        hx.power_up()
except (KeyboardInterrupt, SystemExit):
    GPIO.cleanup()
