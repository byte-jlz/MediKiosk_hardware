"""
sim_hardware.py - fake kiosk sensors for testing WITHOUT the Pi.

Used by kiosk_agent.py when SIMULATE=1. Instead of faking the results, it
fakes the sensor LIBRARIES (smbus, max30102, adafruit_vl53l0x, pyserial,
bp_auto, ...), so the real code in hardware/measurements.py runs exactly as
it does on the Pi - countdowns, confirm steps, RR signal analysis and all.

Every new session gets a new random "person" (height, weight, HR, SpO2,
temperature, breathing rate, BP), so repeated tests look different.

    import sim_hardware
    sim_hardware.install()          # BEFORE the first measurement
    sim_hardware.apply_fast(measurements)   # optional: short scans for testing
    sim_hardware.new_person()       # new random patient
"""

import math
import random
import sys
import time
import types

SIM_MOUNT_MM = 2150          # pretend the ToF sensor is 2.15 m above the floor

PERSON = {}


def new_person():
    PERSON.update(
        height_mm=random.randint(1500, 1820),
        weight_kg=round(random.uniform(48, 92), 1),
        hr=random.randint(62, 96),
        spo2=random.randint(95, 99),
        temp_c=round(random.uniform(36.3, 37.3), 2),
        rr=random.randint(12, 18),
        sys=random.randint(105, 132),
        dia=random.randint(66, 86),
    )
    PERSON["pul"] = PERSON["hr"] + random.randint(-3, 3)   # same heart, two devices

    return dict(PERSON)


new_person()

BP_SECONDS = [40.0]          # how long the fake BP monitor "measures"


# ---------------------------------------------------------------- smbus
class SMBus:
    """MAX30102 part ID + MLX90614 temperature registers."""

    def __init__(self, bus=1):
        pass

    def read_byte_data(self, addr, reg):
        return 0x15 if reg == 0xFF else 0x03          # part ID / "SpO2 mode"

    def read_word_data(self, addr, reg):
        if reg == 0x07:                                 # object (forehead)
            c = PERSON["temp_c"] + random.uniform(-0.05, 0.05)
        else:                                           # ambient
            c = 28.0 + random.uniform(-0.1, 0.1)
        return int((c + 273.15) / 0.02)

    def close(self):
        pass


# ---------------------------------------------------------------- MAX30102
class MAX30102:
    """25 samples/s of a fingertip signal: heartbeat + breathing wave."""

    def __init__(self, channel=1, address=0x57):
        self.bus = SMBus(channel)
        self.n = 0
        self.base = 0
        self.t = time.time()

    def reset(self):
        pass

    def setup(self):
        self.t = time.time()
        self.base = self.n

    def get_data_present(self):
        due = int((time.time() - self.t) * 25) + self.base - self.n
        return max(0, min(due, 32))

    def read_fifo(self):
        k = self.n
        self.n += 1
        s = k / 25
        ir = (100000
              + 800 * math.sin(2 * math.pi * PERSON["hr"] / 60 * s)
              + 1500 * math.sin(2 * math.pi * PERSON["rr"] / 60 * s)
              + random.uniform(-40, 40))
        return int(ir * 0.9), int(ir)

    def shutdown(self):
        pass


def calc_hr_and_spo2(ir, red):
    return (PERSON["hr"] + random.randint(-2, 2), True,
            min(100, PERSON["spo2"] + random.randint(-1, 1)), True)


# ---------------------------------------------------------------- VL53L0X
class I2C:
    def __init__(self, scl=None, sda=None):
        pass

    def deinit(self):
        pass


class VL53L0X:
    def __init__(self, i2c):
        self.measurement_timing_budget = 200000

    @property
    def range(self):
        time.sleep(0.2)                                 # like the 200 ms timing budget
        return SIM_MOUNT_MM - PERSON["height_mm"] + random.randint(-3, 3)


# ---------------------------------------------------------------- scale ESP32
class Serial:
    """Talks like scale_esp32.ino: READY line, W:<kg> 5x/s, T -> OK:TARE."""

    def __init__(self, port=None, baud=115200, timeout=0.1):
        self.out = [b"READY:SCALE factor=19.17\n"]
        self.last = time.time()

    def write(self, data):
        cmd = data.decode(errors="ignore").strip()
        if cmd == "T":
            self.out.append(b"OK:TARE\n")
        elif cmd == "F?":
            self.out.append(b"FACTOR:19.17\n")
        elif cmd == "PING":
            self.out.append(b"PONG:SCALE\n")

    def read(self, n=256):
        time.sleep(0.05)
        if time.time() - self.last >= 0.2:
            self.last = time.time()
            kg = PERSON["weight_kg"] + random.uniform(-0.03, 0.03)
            self.out.append(f"W:{kg:.2f}\n".encode())
        data, self.out = b"".join(self.out), []
        return data

    def readline(self):
        return self.read()

    def reset_input_buffer(self):
        self.out = []

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


# ---------------------------------------------------------------- BP monitor
class _Cam:
    FIELDS = ["SYS", "DIA", "PUL"]
    FRAME_W, FRAME_H = 640, 480
    LIGHT_WARMUP_SEC = 0.2
    settings = {"rois": {"SYS": 1, "DIA": 1, "PUL": 1}, "exposure": 1}

    def open_camera(self, *a):
        class Cap:
            def release(self):
                pass
        return Cap()

    def apply_exposure(self, *a):
        pass


class _Trigger:
    def __init__(self, *a, **kw):
        pass

    def press(self):
        pass

    def led(self, on):
        pass

    def close(self):
        pass


def _bp_measure(cap, trigger, window=False):
    time.sleep(BP_SECONDS[0])
    return {"sys": PERSON["sys"], "dia": PERSON["dia"], "pul": PERSON["pul"],
            "duration_s": BP_SECONDS[0]}


# ---------------------------------------------------------------- install
def _module(name, **attrs):
    mod = types.ModuleType(name)
    mod.__dict__.update(attrs)
    sys.modules[name] = mod
    return mod


def install():
    """Put the fake libraries in place of the real ones (call before measuring)."""
    _module("smbus", SMBus=SMBus)
    _module("max30102", MAX30102=MAX30102)
    _module("hrcalc", calc_hr_and_spo2=calc_hr_and_spo2)
    _module("board", SCL=3, SDA=2)
    _module("busio", I2C=I2C)
    _module("adafruit_vl53l0x", VL53L0X=VL53L0X)
    list_ports = _module("serial.tools.list_ports", comports=lambda: [])
    tools = _module("serial.tools", list_ports=list_ports)
    _module("serial", Serial=Serial, tools=tools)
    _module("cv2")
    _module("bp_auto", cam=_Cam(), PiTrigger=_Trigger, Esp32Trigger=_Trigger,
            ManualTrigger=_Trigger, measure=_bp_measure)


def apply_fast(m):
    """Shorter scans so a full test run takes ~1 minute instead of ~4."""
    m.HR_SECONDS = 6
    m.RR_SECONDS = 24         # measurements needs at least 20 s of samples
    m.HEIGHT_SECONDS = 2
    m.WEIGHT_SECONDS = 2
    m.TEMP_SECONDS = 2
    m.SCALE_BOOT_SEC = 1
    BP_SECONDS[0] = 8.0
