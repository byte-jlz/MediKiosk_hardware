#!/usr/bin/env python3
"""
kiosk_agent.py - the ONE program on the kiosk Pi that talks to the sensors.

Nothing else touches the hardware. The kiosk tablet (and later the backend)
talk to this agent only through MQTT:

    tablet  --cmd-->     agent      "scan bmi" / "confirm" / "cancel"
    tablet  <--state--   agent      countdowns, "put the cuff on", errors
    tablet  <--result--  agent      the final numbers
    tablet  <--live--    agent      ToF + temperature while nobody is scanning
    anyone  <--status--  agent      online/offline (retained, MQTT last-will)

Only one measurement runs at a time, and the live feed pauses during it, so
two programs never fight over I2C bus 1 again. The scale ESP32's USB port is
opened once and kept open (opening it restarts the ESP32, ~8 s).

The topics and every message are documented in agent/README.md.

Run it (from the repo folder, inside the venv):
    python agent/kiosk_agent.py                       # real sensors (on the Pi)
    SIMULATE=1 python agent/kiosk_agent.py            # fake sensors (any laptop)
    SIMULATE=1 SIM_FAST=1 python agent/kiosk_agent.py # fake + short scans
  Windows PowerShell:
    $env:SIMULATE="1"; $env:SIM_FAST="1"; python agent\\kiosk_agent.py

Settings (environment variables, all optional):
    KIOSK_ID          kiosk name used in the topics       (default K-01)
    MQTT_HOST         broker host                         (default localhost)
    MQTT_PORT         broker port                         (default 1883)
    MQTT_USER / MQTT_PASSWORD   broker login, once the broker has a password file
    SIMULATE=1        fake sensors (see agent/sim_hardware.py)
    SIM_FAST=1        with SIMULATE: short scans (HR 6 s, RR 24 s, BP 8 s, ...)
    LIVE=0            turn off the live ToF/temperature feed
    LIVE_INTERVAL     seconds between live readings       (default 0.5)
    CONFIRM_TIMEOUT   seconds to wait for the person to press "ready" (default 120)
    SENSOR_MOUNT_MM   ToF height above the floor, overrides measurements.py
    SCALE_PORT, BP_CAMERA, BP_TRIGGER, BP_PORT   override the measurements.py defaults
"""

import json
import os
import queue
import signal
import sys
import threading
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO_DIR, "hardware"))
sys.path.insert(0, HERE)

try:
    import paho.mqtt.client as mqtt
except ImportError:
    sys.exit("kiosk_agent needs paho-mqtt:  pip install paho-mqtt")


def env_flag(name, default="0"):
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


AGENT_VERSION = "0.1.0"
KIOSK_ID = os.getenv("KIOSK_ID", "K-01")
MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER = os.getenv("MQTT_USER")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD")
SIMULATE = env_flag("SIMULATE")
SIM_FAST = env_flag("SIM_FAST")
LIVE_ENABLED = env_flag("LIVE", "1")
LIVE_INTERVAL = float(os.getenv("LIVE_INTERVAL", "0.5"))
CONFIRM_TIMEOUT = float(os.getenv("CONFIRM_TIMEOUT", "120"))

BASE = f"medikiosk/{KIOSK_ID}"
T_STATUS = f"{BASE}/status"
T_CMD = f"{BASE}/cmd"
T_STATE = f"{BASE}/state"
T_RESULT = f"{BASE}/result"
T_LIVE_TOF = f"{BASE}/live/tof"
T_LIVE_TEMP = f"{BASE}/live/temp"

# The fakes must be in place BEFORE measurements.py imports any driver.
if SIMULATE:
    import sim_hardware
    sim_hardware.install()

import measurements as m          # noqa: E402

if SIMULATE:
    if SIM_FAST:
        sim_hardware.apply_fast(m)
    if m.SENSOR_MOUNT_MM is None:
        m.SENSOR_MOUNT_MM = sim_hardware.SIM_MOUNT_MM
if os.getenv("SENSOR_MOUNT_MM"):
    m.SENSOR_MOUNT_MM = float(os.environ["SENSOR_MOUNT_MM"])

SCALE_PORT = os.getenv("SCALE_PORT") or ("SIM" if SIMULATE else (m.SCALE_PORT if m.ON_PI else None))
BP_CAMERA = os.getenv("BP_CAMERA") or ("0" if SIMULATE else None)
BP_TRIGGER = os.getenv("BP_TRIGGER") or ("manual" if SIMULATE else None)
BP_PORT = os.getenv("BP_PORT")


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} [agent] {msg}", flush=True)


def tidy(value):
    """Round floats so messages stay short (36.590000003 -> 36.59)."""
    if isinstance(value, float):
        return round(value, 3)
    if isinstance(value, dict):
        return {k: tidy(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [tidy(v) for v in value]
    return value


# =====================================================================
# The vitals the agent can measure. Names match the frontend's vitalKey.
# Each runner returns (values, reading, error):
#   values   all the raw numbers from measurements.py (for logs / backend)
#   reading  the value in the shape the frontend wizard already uses
#   error    None if it worked, otherwise a short text for the person
# =====================================================================
def run_height(ctx):
    v = m.measure_height(ctx.confirm, ctx.progress)
    if v["error"]:
        return v, None, v["error"]
    if v["height_cm"] is None:
        return v, None, "height unknown - SENSOR_MOUNT_MM is not set in hardware/measurements.py"
    return v, {"value": round(v["height_cm"], 1), "unit": "cm"}, None


def run_weight(ctx):
    link = ctx.scale()
    try:
        v = m.measure_weight(link, ctx.confirm, ctx.progress)
    except (m.MeasureError, OSError):
        ctx.drop_scale()                    # reopen it next time
        raise
    if v["error"]:
        return v, None, v["error"]
    return v, {"value": round(v["weight_kg"], 2), "unit": "kg"}, None


def run_bmi(ctx):
    """Weight first (tare -> step on), then height while still standing on it."""
    w, w_reading, err = run_weight(ctx)
    values = {"weight": w}
    if err:
        return values, None, err
    h, h_reading, err = run_height(ctx)
    values["height"] = h
    if err:
        return values, None, err
    height_m = h["height_cm"] / 100
    bmi = w["weight_kg"] / (height_m ** 2)
    values.update(weight_kg=w["weight_kg"], height_cm=h["height_cm"], bmi=bmi)
    reading = {"value": round(bmi, 1), "unit": "kg/m²",
               "weight": w_reading, "height": h_reading}
    return values, reading, None


def run_hr_spo2(ctx):
    v = m.measure_hr_spo2(ctx.confirm, ctx.progress)
    hr, spo2 = v["heart_rate_bpm"], v["spo2_pct"]
    error = v["error"]
    if not error and hr is None:
        error = "no valid heart rate reading - keep your finger still and try again"
    if not error and spo2 is None:
        error = "no valid SpO2 reading - keep your finger still and try again"
    if error:
        return v, None, error
    reading = {"heartRate": {"value": round(hr), "unit": "bpm"},
               "spo2": {"value": round(spo2), "unit": "%"}}
    return v, reading, None


def run_temp(ctx):
    v = m.measure_temp(ctx.confirm, ctx.progress)
    if v["error"]:
        return v, None, v["error"]
    return v, {"value": round(v["object_c"], 1), "unit": "°C"}, None


def run_bp(ctx):
    v = m.measure_bp(camera=BP_CAMERA, trigger=BP_TRIGGER, bp_port=BP_PORT,
                     confirm=ctx.confirm, progress=ctx.progress)
    if v["error"]:
        return v, None, v["error"]
    reading = {"systolic": v["sys"], "diastolic": v["dia"], "pulse": v["pul"], "unit": "mmHg"}
    return v, reading, None


def run_rr(ctx):
    v = m.measure_rr(ctx.confirm, ctx.progress)
    if v["level"] == "NONE":
        reasons = v.get("reasons") or []
        return v, None, v["error"] + (": " + "; ".join(reasons) if reasons else "")
    reading = {"value": round(v["rr_bpm"]), "unit": "br/min", "confidence": v["level"]}
    if v["level"] == "ESTIMATE":
        reading.update(low=v["low"], high=v["high"])
    return v, reading, None


VITALS = {
    "bmi": run_bmi,
    "height": run_height,
    "weight": run_weight,
    "heartRateSpo2": run_hr_spo2,
    "temperature": run_temp,
    "bloodPressure": run_bp,
    "respiration": run_rr,
}


# =====================================================================
# One scan request
# =====================================================================
class Job:
    def __init__(self, cmd):
        self.id = str(cmd["id"])
        self.vital = cmd["vital"]
        self.session = cmd.get("session")
        self.auto_confirm = bool(cmd.get("auto_confirm"))
        self.cancel = threading.Event()
        self.confirmed = threading.Event()
        self.waiting_step = None
        self.cancel_reason = "cancelled"


class JobContext:
    """What a runner gets: the two callbacks + access to the scale link."""

    def __init__(self, agent, job):
        self.agent, self.job = agent, job

    def _check_cancel(self):
        if self.job.cancel.is_set():
            raise m.Cancelled(self.job.cancel_reason)

    def confirm(self, step, message):
        job = self.job
        self._check_cancel()
        if job.auto_confirm:
            self.agent.state(job, "info", message=f"(auto-confirmed) {message}")
            return
        job.confirmed.clear()
        job.waiting_step = step
        self.agent.state(job, "awaiting_confirm", step=step, message=message,
                         timeout_s=CONFIRM_TIMEOUT)
        t_end = time.time() + CONFIRM_TIMEOUT
        while not job.confirmed.wait(0.2):
            self._check_cancel()
            if time.time() > t_end:
                job.cancel_reason = f"nobody confirmed '{step}' within {CONFIRM_TIMEOUT:.0f}s"
                raise m.Cancelled(job.cancel_reason)
        job.waiting_step = None
        self.agent.state(job, "confirmed", step=step)

    def progress(self, event):
        self._check_cancel()
        event = dict(event)
        event.pop("vital", None)
        phase = event.pop("phase", "info")
        self.agent.state(self.job, phase, **event)

    def scale(self):
        return self.agent.get_scale(self.progress)

    def drop_scale(self):
        self.agent.drop_scale()


# =====================================================================
# Live ToF + temperature feed (only while no scan is running)
# =====================================================================
class LiveFeed(threading.Thread):
    """Reads the VL53L0X and MLX90614 every LIVE_INTERVAL seconds and publishes
    them in the same JSON format the frontend cards already understand.

    It holds agent.hw_lock while it touches I2C. A scan sets `paused`, takes
    the lock, and calls close() - so the live feed never shares the bus with
    a measurement."""

    def __init__(self, agent):
        super().__init__(daemon=True, name="live-feed")
        self.agent = agent
        self.paused = threading.Event()
        self.stopping = threading.Event()
        self.i2c = self.tof = self.bus = None
        self.retry_at = 0.0
        self.last_problem = None

    # -- sensors --
    def _open(self):
        if time.time() < self.retry_at:
            return
        try:
            if self.tof is None:
                board, busio = __import__("board"), __import__("busio")
                vl53 = __import__("adafruit_vl53l0x")
                self.i2c = busio.I2C(board.SCL, board.SDA)
                self.tof = vl53.VL53L0X(self.i2c)
                self.tof.measurement_timing_budget = m.TOF_TIMING_BUDGET_US
            if self.bus is None:
                self.bus = __import__("smbus").SMBus(m.I2C_BUS)
            self._problem(None)
        except Exception as e:                  # missing library, sensor not answering
            self.close()
            self.retry_at = time.time() + 5
            self._problem(f"live sensors unavailable ({e}) - retrying every 5 s")

    def close(self):
        try:
            if self.i2c is not None:
                self.i2c.deinit()
        except Exception:
            pass
        try:
            if self.bus is not None:
                self.bus.close()
        except Exception:
            pass
        self.i2c = self.tof = self.bus = None

    def _problem(self, text):
        if text != self.last_problem:
            if text:
                log(text)
            self.last_problem = text

    def _read_temp(self, reg):
        for _ in range(5):                      # clock-stretching retries
            try:
                raw = self.bus.read_word_data(m.MLX90614_ADDR, reg)
            except OSError:
                time.sleep(0.05)
                continue
            return None if raw & 0x8000 else raw * 0.02 - 273.15
        return None

    def _read_once(self):
        now = round(time.time(), 3)
        if self.tof is not None:
            try:
                mm = self.tof.range
                in_range = 0 < mm < 8190
                payload = {"sensor": "vl53l0x",
                           "distance_mm": mm if in_range else None,
                           "distance_cm": round(mm / 10, 1) if in_range else None,
                           "in_range": in_range, "simulated": SIMULATE, "ts": now}
                if in_range and m.SENSOR_MOUNT_MM:
                    payload["height_cm"] = round((m.SENSOR_MOUNT_MM - mm) / 10, 1)
                self.agent.publish(T_LIVE_TOF, payload)
            except (OSError, ValueError, RuntimeError) as e:
                self._problem(f"live ToF read failed ({e}) - reopening")
                self.close()
                return
        if self.bus is not None:
            obj, amb = self._read_temp(0x07), self._read_temp(0x06)
            if obj is not None:
                obj += m.TEMP_OFFSET_C
            self.agent.publish(T_LIVE_TEMP, {
                "sensor": "mlx90614",
                "object_c": round(obj, 2) if obj is not None else None,
                "ambient_c": round(amb, 2) if amb is not None else None,
                "valid": obj is not None, "simulated": SIMULATE, "ts": now})

    # -- pause / resume (called by the scan worker) --
    def pause(self):
        self.paused.set()
        self.agent.publish(f"{T_LIVE_TOF}/status", "paused", retain=True, raw=True)
        self.agent.publish(f"{T_LIVE_TEMP}/status", "paused", retain=True, raw=True)

    def resume(self):
        self.paused.clear()
        self.agent.publish(f"{T_LIVE_TOF}/status", "online", retain=True, raw=True)
        self.agent.publish(f"{T_LIVE_TEMP}/status", "online", retain=True, raw=True)

    def run(self):
        self.resume()
        while not self.stopping.is_set():
            started = time.time()
            with self.agent.hw_lock:
                if not self.paused.is_set():
                    if self.tof is None or self.bus is None:
                        self._open()
                    self._read_once()
            self.stopping.wait(max(0.05, LIVE_INTERVAL - (time.time() - started)))
        with self.agent.hw_lock:
            self.close()


# =====================================================================
# The agent
# =====================================================================
class Agent:
    def __init__(self):
        self.hw_lock = threading.Lock()         # whoever holds this may touch I2C
        self.jobs = queue.Queue()
        self.lock = threading.Lock()            # protects self.current
        self.current = None
        self.scale_link = None
        self.last_session = None
        self.stopping = threading.Event()
        self.live = LiveFeed(self) if LIVE_ENABLED else None
        self.client = self._make_client()

    # ---------------- MQTT ----------------
    def _make_client(self):
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                        client_id=f"medikiosk-agent-{KIOSK_ID}-{os.getpid()}")
        if MQTT_USER:
            c.username_pw_set(MQTT_USER, MQTT_PASSWORD)
        # If the agent crashes or the Pi loses power, the broker says so for us.
        c.will_set(T_STATUS, json.dumps({"online": False, "kiosk": KIOSK_ID}), qos=1, retain=True)
        c.on_connect = self._on_connect
        c.on_disconnect = lambda *a: log("MQTT disconnected - retrying...")
        c.on_message = self._on_message
        c.reconnect_delay_set(min_delay=1, max_delay=10)
        return c

    def _on_connect(self, client, _userdata, _flags, reason_code, _props):
        if reason_code.is_failure:
            log(f"MQTT connect failed: {reason_code}")
            return
        log(f"connected to {MQTT_HOST}:{MQTT_PORT} - listening on {T_CMD}")
        client.subscribe(T_CMD, qos=1)
        self.publish_status()

    def publish(self, topic, payload, qos=0, retain=False, raw=False):
        data = payload if raw else json.dumps(tidy(payload))
        return self.client.publish(topic, data, qos=qos, retain=retain)

    def publish_status(self):
        with self.lock:
            job = self.current
        self.publish(T_STATUS, {
            "online": True, "kiosk": KIOSK_ID, "busy": job is not None,
            "current": {"id": job.id, "vital": job.vital, "session": job.session} if job else None,
            "simulated": SIMULATE, "vitals": list(VITALS), "agent_version": AGENT_VERSION,
            "ts": round(time.time(), 3),
        }, qos=1, retain=True)

    def state(self, job, phase, **fields):
        payload = {"id": job.id, "vital": job.vital, "session": job.session,
                   "phase": phase, "ts": round(time.time(), 3), **fields}
        # countdown ticks are many and replaceable -> QoS 0; everything else QoS 1
        self.publish(T_STATE, payload, qos=0 if phase == "measuring" else 1)

    def reject(self, cmd, reason):
        log(f"rejected {cmd.get('action')} {cmd.get('id')}: {reason}")
        self.publish(T_STATE, {"id": cmd.get("id"), "vital": cmd.get("vital"),
                               "session": cmd.get("session"), "phase": "rejected",
                               "message": reason, "ts": round(time.time(), 3)}, qos=1)

    # ---------------- commands ----------------
    def _on_message(self, _client, _userdata, msg):
        """Runs on the MQTT network thread: only check + hand over, never block."""
        try:
            cmd = json.loads(msg.payload.decode())
            if not isinstance(cmd, dict):
                raise ValueError("not a JSON object")
        except (ValueError, UnicodeDecodeError) as e:
            log(f"ignored bad command ({e}): {msg.payload[:100]!r}")
            return
        action = cmd.get("action")
        if action == "scan":
            self._cmd_scan(cmd)
        elif action == "confirm":
            self._cmd_confirm(cmd)
        elif action == "cancel":
            self._cmd_cancel(cmd)
        elif action == "ping":
            self.publish_status()
        else:
            self.reject(cmd, f"unknown action {action!r} (use scan / confirm / cancel / ping)")

    def _cmd_scan(self, cmd):
        if not cmd.get("id"):
            return self.reject(cmd, "scan needs an 'id' (any unique text)")
        if cmd.get("vital") not in VITALS:
            return self.reject(cmd, f"unknown vital {cmd.get('vital')!r} - use one of {list(VITALS)}")
        with self.lock:
            if self.current is not None:
                return self.reject(cmd, f"busy with {self.current.vital} ({self.current.id})")
            job = Job(cmd)
            self.current = job
        self.jobs.put(job)

    def _current_matching(self, cmd):
        with self.lock:
            job = self.current
        if job is None or (cmd.get("id") and str(cmd["id"]) != job.id):
            return None
        return job

    def _cmd_confirm(self, cmd):
        job = self._current_matching(cmd)
        if job is None or job.waiting_step is None:
            return self.reject(cmd, "nothing is waiting for a confirm")
        if cmd.get("step") and cmd["step"] != job.waiting_step:
            return self.reject(cmd, f"waiting for '{job.waiting_step}', not '{cmd['step']}'")
        job.confirmed.set()

    def _cmd_cancel(self, cmd):
        job = self._current_matching(cmd)
        if job is None:
            return self.reject(cmd, "no matching scan is running")
        job.cancel_reason = "cancelled from the kiosk"
        job.cancel.set()

    # ---------------- scale link (kept open) ----------------
    def get_scale(self, progress=None):
        if self.scale_link is None:
            self.scale_link = m.open_scale(SCALE_PORT, progress)
        return self.scale_link

    def drop_scale(self):
        if self.scale_link is not None:
            self.scale_link.close()
        self.scale_link = None

    # ---------------- the worker: one scan at a time ----------------
    def _worker(self):
        try:                                    # open the scale now, not on the first patient
            self.get_scale(lambda e: log(e.get("message", "")) if e.get("message") else None)
            log("scale ESP32 connected")
        except Exception as e:
            log(f"scale not ready yet ({e}) - will retry on the first weight scan")
            self.drop_scale()
        while not self.stopping.is_set():
            try:
                job = self.jobs.get(timeout=0.5)
            except queue.Empty:
                continue
            self._run(job)

    def _run(self, job):
        log(f"scan {job.vital} ({job.id}) started")
        self.publish_status()
        self.state(job, "accepted")
        if SIMULATE and job.session != self.last_session:
            sim_hardware.new_person()           # new session = new pretend patient
        self.last_session = job.session

        ctx = JobContext(self, job)
        values, reading, error, cancelled = None, None, None, False
        started = time.time()
        if self.live:
            self.live.pause()
        try:
            with self.hw_lock:
                if self.live:
                    self.live.close()           # release the bus before measuring
                values, reading, error = VITALS[job.vital](ctx)
        except m.Cancelled as e:
            cancelled, error = True, str(e) or "cancelled"
        except (m.MeasureError, RuntimeError, OSError, SystemExit) as e:
            error = f"sensor problem: {e}"
        except Exception as e:                  # a bug - keep the agent alive, log it
            traceback.print_exc()
            error = f"internal error: {e!r}"
        finally:
            if self.live:
                self.live.resume()
            with self.lock:
                self.current = None

        ok = error is None
        self.state(job, "cancelled" if cancelled else ("finished" if ok else "failed"),
                   message=error or "done")
        self.publish(T_RESULT, {
            "id": job.id, "vital": job.vital, "session": job.session, "kiosk": KIOSK_ID,
            "ok": ok, "cancelled": cancelled, "error": error,
            "reading": reading, "values": values,
            "simulated": SIMULATE, "duration_s": time.time() - started,
            "ts": round(time.time(), 3),
        }, qos=1)
        self.publish_status()
        log(f"scan {job.vital} ({job.id}) -> " + (json.dumps(tidy(reading)) if ok else f"no result: {error}"))

    # ---------------- start / stop ----------------
    def run_forever(self):
        log(f"MediKiosk agent {AGENT_VERSION} - kiosk {KIOSK_ID}"
            + (" - SIMULATED sensors" + (" (fast)" if SIM_FAST else "") if SIMULATE else ""))
        self.client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=30)
        self.client.loop_start()
        worker = threading.Thread(target=self._worker, daemon=True, name="scan-worker")
        worker.start()
        if self.live:
            self.live.start()
        try:
            while not self.stopping.wait(0.5):
                pass
        finally:
            log("stopping...")
            with self.lock:
                job = self.current
            if job:
                job.cancel_reason = "the kiosk agent is shutting down"
                job.cancel.set()
                worker.join(timeout=5)
            if self.live:
                self.live.stopping.set()
                self.live.join(timeout=3)
                for t in (T_LIVE_TOF, T_LIVE_TEMP):
                    self.publish(f"{t}/status", "offline", retain=True, raw=True)
            self.drop_scale()
            self.publish(T_STATUS, {"online": False, "kiosk": KIOSK_ID}, qos=1,
                         retain=True).wait_for_publish(timeout=2)
            self.client.loop_stop()
            self.client.disconnect()

    def stop(self, *_):
        self.stopping.set()


def main():
    agent = Agent()
    signal.signal(signal.SIGINT, agent.stop)
    signal.signal(signal.SIGTERM, agent.stop)
    agent.run_forever()


if __name__ == "__main__":
    main()
