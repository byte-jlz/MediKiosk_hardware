#!/usr/bin/env python3
"""
test_relay.py - Pi version of the ESP32 relay + 6 LED test.

Wiring (see the wiring diagram):
  relay VCC -> 3.3V (pin 17)      relay GND -> GND (pin 20)
  relay IN  -> GPIO26 (pin 37)
  5V (pin 2) -> relay COM         relay NO  -> LED + rail
  LED long leg -> 100 ohm -> + rail,   LED short leg -> GND rail -> GND (pin 39)

Run:  python test_relay.py
You should hear the relay click and the LEDs blink 1 s on / 1 s off.
If they're ON when it says OFF, set RELAY_ACTIVE_LOW = False in pi_pins.py.
"""

import time

try:
    from hardware import pi_pins
except ImportError:
    import pi_pins

print(f"Blinking lights on GPIO{pi_pins.LIGHT_RELAY} - Ctrl+C to stop")
try:
    while True:
        pi_pins.lights(True)
        print("ON")
        time.sleep(1)
        pi_pins.lights(False)
        print("OFF")
        time.sleep(1)
except KeyboardInterrupt:
    pass
finally:
    pi_pins.lights(False)          # always leave the lights off
    print("\nStopped (lights off).")
