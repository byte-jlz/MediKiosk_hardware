#!/usr/bin/env python3
"""
pi_pins.py - one place for every Raspberry Pi 4B pin MediKiosk uses,
plus a tiny pinctrl helper for the two output pins (BP button + light relay).

These replace the two jobs the ESP32 used to do:
  ESP32 "PRESS <ms>"        ->  press_bp_button(ms)     (GPIO27, pin 13)
  ESP32 "LED_ON"/"LED_OFF"  ->  lights(True/False)      (GPIO26, pin 37, relay IN)

Physical pin map (BCM numbers in code, board pin in brackets):
  3.3V                [1]  -> VL53L0X, MAX30102, MLX90614 VIN
  3.3V                [17] -> HX711 VCC, relay VCC
  5V                  [2]  -> relay COM (LED supply)
  GPIO2  SDA          [3]  -> I2C SDA (all three I2C sensors)
  GPIO3  SCL          [5]  -> I2C SCL (all three I2C sensors)
  GPIO5               [29] -> HX711 DT/DOUT
  GPIO6               [31] -> HX711 SCK
  GPIO27              [13] -> BP monitor START button line
                             (GPIO17 / pin 11 is damaged since 2026-10-31 - do not use)
  GPIO26              [37] -> relay IN (camera lights)
  GND                 [6], [9], [14], [20], [25], [30], [34], [39]
"""

import subprocess
import time

# ---- pin numbers (BCM) ----
I2C_SDA = 2
I2C_SCL = 3
HX711_DT = 5
HX711_SCK = 6
BP_BUTTON = 27   # was 17 (pin 11) - that pin got damaged 2026-10-31
LIGHT_RELAY = 26

# ---- I2C addresses (all on bus 1) ----
ADDR_VL53L0X = 0x29
ADDR_MAX30102 = 0x57
ADDR_MLX90614 = 0x5A

# Most blue relay boards switch ON when IN is pulled LOW ("low-level trigger").
# If your lights come on when they should be off, set this to False.
RELAY_ACTIVE_LOW = True


def pinctrl(pin, *args):
    """Run `pinctrl set <pin> ...` (same tool bp_bttn_auto.py already uses)."""
    r = subprocess.run(["pinctrl", "set", str(pin), *args],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"pinctrl failed on GPIO{pin}: {r.stderr.strip()}")


# ---------------- BP monitor START button ----------------
def release_bp_button(pin=BP_BUTTON):
    """Let go of the button line (input, NO pull = not pressed).
    The monitor has its own pull-up (~3.0 V), so the Pi must not add its 3.3 V one."""
    pinctrl(pin, "ip", "pn")


def press_bp_button(ms=900, pin=BP_BUTTON):
    """Same as the ESP32's PRESS <ms>: hold the line LOW, then release."""
    pinctrl(pin, "op", "dl")
    time.sleep(ms / 1000)
    release_bp_button(pin)


# ---------------- Camera light relay ----------------
def lights(on, pin=LIGHT_RELAY, active_low=RELAY_ACTIVE_LOW):
    """Same as the ESP32's LED_ON / LED_OFF: switch the relay feeding the LEDs."""
    drive_low = on if active_low else not on
    pinctrl(pin, "op", "dl" if drive_low else "dh")
