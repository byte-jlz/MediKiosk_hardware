#!/usr/bin/env python3
"""
MLX90614 (GY-906) contactless temperature reader

Wiring (Raspberry Pi 4B, 3.3V I2C):
    VIN/VCC -> 3.3V   (pin 1)
    GND     -> GND    (pin 6)
    SCL     -> GPIO3  (pin 5)
    SDA     -> GPIO2  (pin 3)
"""

import time
import smbus

I2C_BUS = 1
MLX90614_ADDR = 0x5A

REG_AMBIENT = 0x06    # TA    - ambient (sensor die) temperature
REG_OBJECT = 0x07     # TOBJ1 - object (target) temperature

bus = smbus.SMBus(I2C_BUS)


def read_temp_c(register, retries=5):
    """Read a temperature register in °C, retrying on transient I2C errors."""
    for _ in range(retries):
        try:
            raw = bus.read_word_data(MLX90614_ADDR, register)
        except OSError:
            time.sleep(0.05)      # brief pause, then retry
            continue
        if raw & 0x8000:          # bit 15 set = invalid / out-of-range
            return None
        return raw * 0.02 - 273.15
    return None                   # all retries exhausted


def read_ambient_c():
    return read_temp_c(REG_AMBIENT)


def read_object_c():
    return read_temp_c(REG_OBJECT)


if __name__ == "__main__":
    print("MLX90614 temperature reader — Ctrl+C to stop\n")
    try:
        while True:
            ambient = read_ambient_c()
            obj = read_object_c()
            if obj is None or ambient is None:
                print("Reading unavailable (retries exhausted)")
            else:
                print(f"Ambient: {ambient:6.2f} °C    Object: {obj:6.2f} °C")
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        bus.close()