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

The fake SCALE behaves like the real platform (Nov 2026, auto-zero):
  - the pretend patient steps ON when their session starts (they stand on the
    platform while scanning the QR code) and steps OFF ~25 s after their last
    scan - so the agent's idle auto-zero sees an empty platform between patients
  - "step off" / "step on" prompts: the pretend patient obeys after ~2 s
  - stepping on/off wobbles the reading for ~1 s, like a real person does
Test the safety fallback ("please step off") with these (PowerShell, before start_sim.ps1):
    $env:SIM_SCALE_DRIFT_KG="3"    # the zero drifts 3 kg every time a patient leaves
    $env:SIM_START_ON_PLATFORM="1" # someone is already standing on it when the agent starts

The fake FINGER sensor (SpO2 & RR step) - pick what the pretend patient does:
    $env:SIM_FINGER="lift"         # normal | slip | lift | lift_always | move | never | fault

The fake BP MONITOR:
    $env:SIM_BP="error"            # normal | error (monitor error screen) | timeout (no answer)

The fake TEMPERATURE sensor:
    $env:SIM_TEMP="shaky"          # normal | far | wobble | shaky | shaky_always | low | hot |
                                   # never | fault | hotroom
"""

import os

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
    _move(True, wobble=False)                               # stands on the platform for the QR scan
    PLATFORM["leave_at"] = None
    return dict(PERSON)


# ---------------------------------------------------------------- platform (who is on the scale)
PLATFORM = {
    "on": os.getenv("SIM_START_ON_PLATFORM", "0") == "1",
    "raw_offset": 0.0,        # load-cell drift (kg) - raw reading with nobody on it
    "tare": 0.0,              # what the ESP32 subtracts (set by T)
    "wobble_until": 0.0,      # stepping on/off shakes the reading
    "pending": None,          # (time, on?) - the pretend patient's next move
    "leave_at": None,
    "busy": False,
}
DRIFT_KG = float(os.getenv("SIM_SCALE_DRIFT_KG", "0") or 0)
PROMPT_DELAY_SEC = 2.0        # the pretend patient reacts to a prompt after this long
LEAVE_AFTER_SEC = 25.0        # ...and leaves this long after their last scan


def _move(on, wobble=True):
    if PLATFORM["on"] != on:
        PLATFORM["on"] = on
        if wobble:
            PLATFORM["wobble_until"] = time.time() + 1.0
        if not on and DRIFT_KG:
            PLATFORM["raw_offset"] += DRIFT_KG       # the zero drifts while empty
            print(f"[sim] the scale's zero drifted by {DRIFT_KG:+.1f} kg")
    PLATFORM["pending"] = None


def _update_platform():
    now = time.time()
    pend = PLATFORM["pending"]
    if pend and now >= pend[0]:
        _move(pend[1])
    if PLATFORM["leave_at"] and now >= PLATFORM["leave_at"] and not PLATFORM["busy"]:
        PLATFORM["leave_at"] = None
        _move(False)
        print("[sim] the pretend patient stepped off the platform")


def on_prompt(step):
    """The screen asked the person to do something - the pretend patient obeys."""
    if step in ("finger_rest", "finger_again"):
        _finger_prompt()
    elif step in ("forehead", "temp_again"):
        if step == "forehead" or WRIST["on_at"] is None:
            _wrist_prompt()
        else:
            WRIST["attempt"] += 1                     # restart: the wrist is still there
            WRIST["anchor"] = time.time()
    elif step == "scale_empty":
        PLATFORM["pending"] = (time.time() + PROMPT_DELAY_SEC, False)
    elif step == "step_on":
        PLATFORM["pending"] = (time.time() + PROMPT_DELAY_SEC, True)


def job_started(vital=None):
    PLATFORM["busy"] = True
    PLATFORM["leave_at"] = None
    if vital in ("bmi", "weight") and not PLATFORM["on"] and PLATFORM["pending"] is None:
        _move(True, wobble=False)          # a guest also stands on the platform when they tap Start


def job_finished():
    FINGER["on_at"] = FINGER["anchor"] = None   # the hand leaves the finger sensor
    WRIST["on_at"] = WRIST["anchor"] = None     # ...and the wrist leaves the temperature sensor
    PLATFORM["busy"] = False
    PLATFORM["leave_at"] = time.time() + LEAVE_AFTER_SEC


def _scale_raw():
    _update_platform()
    kg = PLATFORM["raw_offset"] + (PERSON.get("weight_kg", 60) if PLATFORM["on"] else 0.0)
    if time.time() < PLATFORM["wobble_until"]:
        kg += random.uniform(-4, 4)
    return kg + random.uniform(-0.03, 0.03)


BP_SECONDS = [40.0]          # how long the fake BP monitor "measures"


new_person()
_move(os.getenv("SIM_START_ON_PLATFORM", "0") == "1")   # nobody on it at start (unless asked)

# ---------------------------------------------------------------- smbus
class SMBus:
    """MAX30102 part ID + MLX90614 temperature registers."""

    def __init__(self, bus=1):
        pass

    def read_byte_data(self, addr, reg):
        return 0x15 if reg == 0xFF else 0x03          # part ID / "SpO2 mode"

    def read_word_data(self, addr, reg):
        c = _temp_reading(reg)                          # 0x07 object (wrist), 0x06 ambient (chip)
        return max(0, int((c + 273.15) / 0.02))

    def close(self):
        pass


# ---------------------------------------------------------------- temperature (MLX90614)
# What the pretend patient does at the temperature sensor - SIM_TEMP:
#   normal        holds the wrist still at 2-5 cm
#   far           pulls the wrist away for ~1.5 s mid-reading ("move closer" warning only)
#   wobble        the wrist wobbles a little the whole time ("hold still" warning only)
#   shaky         wobbles a lot on the first try (-> restarts by itself), then holds still
#   shaky_always  wobbles a lot every try (-> restart limit, "ask the staff")
#   low           holds the wrist too far away (-> result below 35 C, "try again")
#   hot           a hot cup in front of the sensor (-> above 43 C, "doesn't look right")
#   never         no wrist ever (-> "Are you still there?")
#   fault         the sensor's chip temperature is impossible (-> sensor problem)
#   hotroom       a 32.5 C room - detection only works through the hot-room rule
TEMP_SCENARIO = os.getenv("SIM_TEMP", "normal").strip().lower()
TEMP_RAW_OFFSET = 2.9         # measurements.TEMP_OFFSET_C: the sensor reads the wrist this much lower
WRIST = {"on_at": None, "anchor": None, "attempt": 0}


def _wrist_prompt():
    WRIST["attempt"] += 1
    if TEMP_SCENARIO == "never":
        return
    WRIST["on_at"] = time.time() + PROMPT_DELAY_SEC
    WRIST["anchor"] = WRIST["on_at"]


def _temp_reading(reg):
    now = time.time()
    room = 32.5 if TEMP_SCENARIO == "hotroom" else 28.0
    if reg != 0x07:                                   # the sensor's own chip temperature
        return -273.15 if TEMP_SCENARIO == "fault" else room + random.uniform(-0.1, 0.1)
    on = WRIST["on_at"] is not None and now >= WRIST["on_at"]
    t = now - WRIST["anchor"] if on else 0
    if TEMP_SCENARIO == "far" and 2.0 <= t < 3.5:
        on = False                                    # pulled away for a moment
    if not on:
        return room + random.uniform(-0.2, 0.2)       # the sensor just sees the room
    if TEMP_SCENARIO == "hot":
        return 49.0 + random.uniform(-0.2, 0.2)
    skin = PERSON["temp_c"] - TEMP_RAW_OFFSET
    if TEMP_SCENARIO == "low":
        skin = 31.6                                   # too far away: reads cool (still > room)
    if TEMP_SCENARIO == "hotroom":
        skin = max(skin, 34.0)
    wobble = 0.05
    if TEMP_SCENARIO == "wobble":
        wobble = 0.35                                 # a little shaky: warning only
    if TEMP_SCENARIO == "shaky_always" or (TEMP_SCENARIO == "shaky" and WRIST["attempt"] == 1):
        wobble = 1.0                                  # very shaky: unusable -> restart
    return skin + random.uniform(-wobble, wobble)


# ---------------------------------------------------------------- finger (MAX30102)
# How the pretend patient uses the finger sensor - SIM_FINGER (see the top of this file):
#   normal       rests the finger 2 s after the prompt and keeps it still
#   slip         lifts it for 1 s in the middle (warning only, reading continues)
#   lift         lifts it for 3.5 s on the first try (-> restart), then behaves
#   lift_always  lifts it on every try (-> restart limit reached)
#   move         keeps it on but moves the hand for 5 s (movement hint)
#   never        never puts a finger on (-> "Are you still there?")
#   fault        the sensor is dead: IR stuck at 0 (-> sensor problem)
FINGER_SCENARIO = os.getenv("SIM_FINGER", "normal").strip().lower()
FINGER = {"on_at": None, "anchor": None, "attempt": 0}


def _finger_window(start, end):
    a = FINGER["anchor"]
    return a is not None and a + start <= time.time() < a + end


def _finger_lifted():
    s, n = FINGER_SCENARIO, FINGER["attempt"]
    if s == "slip":
        return _finger_window(12, 13)
    if s == "lift" and n == 1 or s == "lift_always":
        return _finger_window(12, 15.5)
    return False


def _finger_on():
    on_at = FINGER["on_at"]
    return on_at is not None and time.time() >= on_at and not _finger_lifted()


def _finger_prompt():
    """finger_rest / finger_again: the pretend patient (re)places the finger."""
    FINGER["attempt"] += 1
    if FINGER_SCENARIO == "never":
        return
    if _finger_on():
        FINGER["anchor"] = time.time()        # already there - this try starts now
        return
    FINGER["on_at"] = time.time() + PROMPT_DELAY_SEC
    FINGER["anchor"] = FINGER["on_at"]


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
        if FINGER_SCENARIO == "fault":
            return 0, 0                                  # dead sensor
        if not _finger_on():
            ir = 3000 + random.uniform(-300, 300)        # no finger: hardly any light comes back
            return int(ir * 0.9), int(ir)
        ir = (100000
              + 800 * math.sin(2 * math.pi * PERSON["hr"] / 60 * s)
              + 1500 * math.sin(2 * math.pi * PERSON["rr"] / 60 * s)
              + random.uniform(-40, 40))
        if FINGER_SCENARIO == "move" and _finger_window(10, 15):
            ir *= 1 + 0.35 * math.sin(2 * math.pi * 1.3 * s)   # the hand rocks on the sensor
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
    def __init__(self, i2c, address=0x29, io_timeout_s=0):
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
            PLATFORM["tare"] = _scale_raw()           # whatever is on it now becomes 0
            self.out.append(b"OK:TARE\n")
        elif cmd == "F?":
            self.out.append(b"FACTOR:19.17\n")
        elif cmd == "PING":
            self.out.append(b"PONG:SCALE\n")

    def read(self, n=256):
        time.sleep(0.05)
        if time.time() - self.last >= 0.2:
            self.last = time.time()
            kg = _scale_raw() - PLATFORM["tare"]
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


# What the pretend BP monitor does - SIM_BP:
#   normal    a reading (pumping -> measuring -> checking -> result)
#   error     the monitor shows its own error screen (Er P = cuff too loose / arm moved)
#   timeout   the monitor never gives a result (-> "monitor isn't responding")
BP_SCENARIO = os.getenv("SIM_BP", "normal").strip().lower()


def _bp_measure(cap, trigger, window=False, on_tick=None):
    t0 = time.time()
    total = BP_SECONDS[0]
    stop_at = total * 0.5 if BP_SCENARIO == "error" else total
    while time.time() - t0 < stop_at:
        el = time.time() - t0
        phase = ("pumping" if el < total * 0.3 else
                 "checking" if el > total * 0.85 and BP_SCENARIO == "normal" else "measuring")
        if on_tick:
            on_tick(round(el, 1), phase)
        time.sleep(min(1.0, total))
    if BP_SCENARIO == "error":
        return {"error": "Er P", "message": "The BP monitor showed an error. Please check the cuff."}
    if BP_SCENARIO == "timeout":
        return None
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
    m.TEMP_SECONDS = 5        # keep the real 5 s: the wobble checks need it
    m.SCALE_BOOT_SEC = 1
    BP_SECONDS[0] = 8.0
