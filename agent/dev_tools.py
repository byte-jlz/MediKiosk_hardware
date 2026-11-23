"""
dev_tools.py - the kiosk's Developer options (developer PIN only), agent side.

kiosk_agent.py checks the developer session token first, then hands every
`dev_*` command to DevTools.handle(). Answers go to medikiosk/<id>/system
(same as shutdown / staff_pin); live data for the open screen goes to:

    medikiosk/<id>/dev                  JSON  {"kind": "weight" | "bpcam", ...}
    medikiosk/<id>/dev/bpcam/preview    raw JPEG of the BP camera (while its screen is open)

Commands (all carry "token" from the staff_pin answer):

    dev_cal_get                          current calibration numbers (calibration.json or default)
    dev_cal_set   {values: {KEY: n}}     save to hardware/calibration.json and use it right away
    dev_scale     {op: watch|stop|tare|calibrate|save|restore, kg, factor}
    dev_bpcam     {op: start|stop|keepalive|set|save|reset|light, settings, on}
    dev_shutdown                         turn the Pi off (same as the staff shutdown, no second PIN)
    dev_wifi      {op: status|scan|connect, ssid, password}   (wifi_tools.py, nmcli on the Pi)
    dev_system    {op: restart|desktop|terminal, stop}         (kiosk_control.sh, started detached)

Temperature uses the live/temp feed (it now also sends raw_c, before the offset).
SpO2 and height use normal scans from the screen (no session = nothing saved).
"""

import copy
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections import deque

from wifi_tools import Wifi

# what the screens may change, and the sane range for each (anything else is refused)
CAL_LIMITS = {
    "TEMP_OFFSET_C": (-2.0, 8.0),
    "SPO2_OFFSET": (-10.0, 15.0),
    "SENSOR_MOUNT_MM": (1000, 2600),
    "TOF_SCALE": (0.9, 1.1),
}
SCALE_WATCH_S = 180          # live weight on the screen stops by itself after this
SCALE_WATCH_EVERY_S = 0.3
BPCAM_IDLE_STOP_S = 120      # no dev_bpcam command this long -> camera closed (screen gone)
BPCAM_PUBLISH_EVERY_S = 0.3
BPCAM_PREVIEW_W = 640
BPCAM_JPEG_QUALITY = 70
STABLE_FRAMES = 5            # same as bp_camTEST.py: same text 5 frames in a row = steady


class DevTask:
    """A developer job for the scan worker thread (the only thread that may use the scale)."""

    def __init__(self, fn):
        self.fn = fn


class DevTools:
    def __init__(self, agent, m, log, simulate, real_cv2, base_topic):
        self.agent, self.m, self.log = agent, m, log
        self.t_system = f"{base_topic}/system"
        self.simulate, self.real_cv2 = simulate, real_cv2
        self.t_dev = f"{base_topic}/dev"
        self.t_preview = f"{base_topic}/dev/bpcam/preview"
        self.scale_watch_until = 0.0
        self.scale_old_factor = None          # the factor before this screen changed it
        self._last_watch_pub = 0.0
        self.bpcam = None                     # BpCamCalibrator while its screen is open
        self._cam_module = None
        self.wifi = Wifi(simulate, m.ON_PI, os.path.dirname(m.HW_DIR), log)
        self._wifi_busy = threading.Lock()    # one scan / connect at a time

    # ---------------- helpers ----------------
    def reply(self, cmd, ok, message="", **extra):
        payload = {"id": cmd.get("id"), "action": cmd.get("action"), "ok": ok,
                   "message": message, "ts": round(time.time(), 3), **extra}
        self.agent.publish(self.t_system, payload, qos=1)

    def busy(self):
        with self.agent.lock:
            return self.agent.current is not None

    # ---------------- entry point (MQTT thread: never block) ----------------
    def handle(self, cmd):
        action = cmd.get("action")
        try:
            if action == "dev_cal_get":
                return self.cal_get(cmd)
            if action == "dev_cal_set":
                return self.cal_set(cmd)
            if action == "dev_scale":
                return self.scale_cmd(cmd)
            if action == "dev_bpcam":
                return self.bpcam_cmd(cmd)
            if action == "dev_shutdown":
                return self.shutdown(cmd)
            if action == "dev_wifi":
                return self.wifi_cmd(cmd)
            if action == "dev_system":
                return self.system_cmd(cmd)
            return self.reply(cmd, False, f"unknown developer command {action!r}")
        except Exception as e:                       # never let a bad command kill the agent
            self.log(f"developer command {action} failed: {e!r}")
            return self.reply(cmd, False, f"failed: {e}")

    def end_session(self):
        """Developer options closed (or the session timed out): stop anything still running."""
        self.scale_watch_until = 0.0
        if self.bpcam is not None:
            self.bpcam.stop()
            self.bpcam = None

    # ---------------- shut down ----------------
    def shutdown(self, cmd):
        """Developer options' Shut down button: the developer PIN already let us in, so
        no shutdown PIN here. Same clean shutdown as the staff panel."""
        if self.busy():
            return self.reply(cmd, False, "A scan is running. Wait for it to finish.")
        self.end_session()                   # camera / live weight off first
        self.log("developer options: shut down requested")
        # not on the MQTT thread: sudo + shutdown may take a moment
        threading.Thread(target=self.agent._shutdown_pi, args=(cmd,), daemon=True, name="shutdown").start()

    # ---------------- restart / exit to desktop / terminal ----------------
    def system_cmd(self, cmd):
        """Hand over to kiosk_control.sh, DETACHED (its own session): restart and
        'desktop --stop' also stop this agent, and the script must outlive it."""
        op = cmd.get("op")
        if op not in ("restart", "desktop", "terminal"):
            return self.reply(cmd, False, f"unknown system op {op!r}")
        if self.busy():
            return self.reply(cmd, False, "A scan is running. Wait for it to finish.")
        args = [op] + (["--stop"] if op == "desktop" and cmd.get("stop") else [])
        if self.simulate or not self.m.ON_PI:
            self.log(f"developer options: kiosk_control.sh {' '.join(args)} - NOT run (simulation / not the Pi)")
            return self.reply(cmd, True, "simulated")
        script = os.path.join(os.path.dirname(self.m.HW_DIR), "kiosk_control.sh")
        if not os.path.exists(script):
            return self.reply(cmd, False, f"{script} is missing - git pull the hardware repo")
        self.end_session()                   # camera / live weight off first
        self.log(f"developer options: kiosk_control.sh {' '.join(args)} (log: ~/medikiosk-logs/control.log)")
        # answer FIRST: the script waits 1 s, then may stop this agent
        self.reply(cmd, True, {"restart": "restarting", "desktop": "leaving", "terminal": "leaving"}[op])
        subprocess.Popen(["bash", script, *args], start_new_session=True, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # ---------------- Wi-Fi (slow: runs in its own thread) ----------------
    def wifi_cmd(self, cmd):
        op = cmd.get("op")
        if op not in ("status", "scan", "connect"):
            return self.reply(cmd, False, f"unknown wifi op {op!r}")
        if op == "connect":
            ssid = str(cmd.get("ssid") or "")
            password = str(cmd.get("password") or "")
            if not ssid or len(ssid.encode()) > 32:
                return self.reply(cmd, False, "pick a network first")
            if password and not 8 <= len(password) <= 63:
                return self.reply(cmd, False, "a Wi-Fi password is 8 to 63 characters")
            if self.busy():
                return self.reply(cmd, False, "A scan is running. Wait for it to finish.")
        threading.Thread(target=self._wifi_task, args=(cmd, op), daemon=True, name=f"wifi-{op}").start()

    def _wifi_task(self, cmd, op):
        if not self._wifi_busy.acquire(timeout=60):
            return self.reply(cmd, False, "Wi-Fi is still busy with the last request.")
        try:
            if op == "status":
                return self.reply(cmd, True, "ok", **self.wifi.status())
            if op == "scan":
                return self.reply(cmd, True, "ok", networks=self.wifi.scan())
            ssid = str(cmd["ssid"])
            self.log(f"developer options: connecting Wi-Fi to {ssid!r}")      # never the password
            ok, message = self.wifi.connect(ssid, str(cmd.get("password") or ""))
            self.log(f"developer options: Wi-Fi {ssid!r}: {message}")
            return self.reply(cmd, ok, message, **(self.wifi.status() if ok else {}))
        except Exception as e:
            self.log(f"developer options: Wi-Fi {op} failed: {e!r}")
            return self.reply(cmd, False, f"Wi-Fi problem: {e}")
        finally:
            self._wifi_busy.release()

    # ---------------- calibration.json ----------------
    def cal_values(self):
        m = self.m
        status = m.calibration_status()
        return ({k: getattr(m, k) for k in m.CALIBRATED_KEYS},
                {k: status[k][1] for k in m.CALIBRATED_KEYS})

    def cal_get(self, cmd):
        values, sources = self.cal_values()
        self.reply(cmd, True, "ok", values=values, sources=sources,
                   defaults=dict(self.m.CALIBRATION_DEFAULTS), limits=CAL_LIMITS)

    def cal_set(self, cmd):
        values = cmd.get("values")
        if not isinstance(values, dict) or not values:
            return self.reply(cmd, False, "nothing to save")
        clean = {}
        for key, v in values.items():
            if key not in CAL_LIMITS:
                return self.reply(cmd, False, f"{key} can't be changed here")
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                return self.reply(cmd, False, f"{key} must be a number")
            lo, hi = CAL_LIMITS[key]
            if not lo <= v <= hi:
                return self.reply(cmd, False, f"{key} = {v} is outside {lo} to {hi} - typo?")
            clean[key] = round(float(v), 4) if key != "SENSOR_MOUNT_MM" else int(round(v))
        if self.busy():
            return self.reply(cmd, False, "A scan is running. Wait for it to finish.")
        note = str(cmd.get("note") or "kiosk developer options")[:80]
        self.m.save_calibration(clean, note=note)
        self.log(f"developer options: saved {clean} ({note})")
        values, sources = self.cal_values()
        self.reply(cmd, True, "saved", values=values, sources=sources)

    # ---------------- scale (runs on the worker thread) ----------------
    def scale_cmd(self, cmd):
        op = cmd.get("op")
        if op == "stop":
            self.scale_watch_until = 0.0
            return self.reply(cmd, True, "stopped")
        if op not in ("watch", "tare", "calibrate", "save", "restore"):
            return self.reply(cmd, False, f"unknown scale op {op!r}")
        if self.busy():
            return self.reply(cmd, False, "A scan is running. Wait for it to finish.")
        if op == "calibrate":
            try:
                kg = float(cmd.get("kg"))
            except (TypeError, ValueError):
                return self.reply(cmd, False, "type the known weight in kg")
            if not 1 <= kg <= 200:
                return self.reply(cmd, False, "the known weight must be 1-200 kg (heavier is better)")
            cmd["kg"] = kg
        self.agent.jobs.put(DevTask(lambda: self._scale_task(cmd, op)))

    def _scale_task(self, cmd, op):
        m = self.m
        try:
            link = self.agent.get_scale()
            if op == "watch":
                if self.scale_old_factor is None:
                    link.clear_line()
                    self.scale_old_factor = link.factor() or link.ready_factor
                self.scale_watch_until = time.time() + SCALE_WATCH_S
                return self.reply(cmd, True, "watching", factor=self.scale_old_factor)
            self.scale_watch_until = time.time() + SCALE_WATCH_S     # keep the live weight going
            link.clear_line()
            if op == "tare":
                link.tare()
                ok = link.verify_zero()
                return self.reply(cmd, True, "tared" if ok else "tared, but the reading moved - tare again",
                                  steady=ok)
            if op == "calibrate":
                link.read_kg(m.CAL_SETTLE_SEC)                       # let the known weight settle
                link.send(f"C:{cmd['kg']}")
                got = link.wait_for("CAL:", 10)
                if got is None:
                    return self.reply(cmd, False, "the scale did not answer the calibration")
                factor = float(got)
                ratio = abs(factor) / m.CAL_EXPECTED_PER_G
                warn = "" if 0.67 <= ratio <= 1.5 else (
                    "far from the usual ~19.17 - was the platform empty at step 1, the weight "
                    "centred, and the number in kg?")
                self.log(f"developer options: scale calibrated with {cmd['kg']} kg -> factor {factor:.4f} "
                         "(not saved yet)")
                return self.reply(cmd, True, "calibrated", factor=factor, warning=warn)
            if op == "save":
                link.send("S")
                got = link.wait_for("OK:SAVED", 3)
                if got is None:
                    return self.reply(cmd, False, "the scale did not confirm the save")
                self.scale_old_factor = float(got) if got else self.scale_old_factor
                self.log(f"developer options: scale factor saved in the ESP32 ({got})")
                return self.reply(cmd, True, "saved", factor=self.scale_old_factor)
            if op == "restore":
                old = cmd.get("factor") or self.scale_old_factor
                if not old:
                    return self.reply(cmd, False, "the old factor is unknown")
                link.send(f"F:{float(old)}")
                link.wait_for("FACTOR:", 3)
                self.log(f"developer options: scale factor put back to {float(old):.4f}")
                return self.reply(cmd, True, "restored", factor=float(old))
        except (m.MeasureError, OSError, ValueError) as e:
            self.agent.drop_scale()
            self.reply(cmd, False, f"scale problem: {e}")

    def scale_idle(self, link):
        """Called by the worker instead of auto-zero while the weight screen is open.
        Returns True if it handled the idle tick (so auto-zero must NOT run - it would
        re-tare in the middle of a calibration)."""
        if time.time() > self.scale_watch_until:
            return False
        vals = link.read_kg(SCALE_WATCH_EVERY_S)
        if vals and time.time() - self._last_watch_pub >= SCALE_WATCH_EVERY_S:
            self._last_watch_pub = time.time()
            spread = max(vals) - min(vals) if len(vals) > 1 else 0.0
            self.agent.publish(self.t_dev, {"kind": "weight", "kg": round(vals[-1], 2),
                                            "spread_kg": round(spread, 2), "ts": round(time.time(), 3)})
        return True

    # ---------------- BP camera ----------------
    def cam_module(self):
        """bp_camTEST.py (the same box drawing / digit reading the BP scan uses)."""
        if self._cam_module is not None:
            return self._cam_module
        m = self.m
        if not self.simulate:
            # the SAME module object bp_auto uses -> saving updates the next BP scan too
            self._cam_module = m.load_bp().cam
            return self._cam_module
        # SIMULATE: cv2 and bp_auto are fakes, so load the real file with the real OpenCV
        if self.real_cv2 is None:
            raise RuntimeError("OpenCV is not installed (pip install opencv-python)")
        path = os.path.join(m.HW_DIR, "bp_camTEST.py")
        fake_cv2 = sys.modules.get("cv2")
        old_cwd = os.getcwd()
        sys.modules["cv2"] = self.real_cv2
        os.chdir(m.HW_DIR)                   # it loads bp_cam_settings.json from here
        try:
            spec = importlib.util.spec_from_file_location("bp_camTEST_dev", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        finally:
            os.chdir(old_cwd)
            if fake_cv2 is not None:
                sys.modules["cv2"] = fake_cv2
        self._cam_module = mod
        return mod

    def bpcam_cmd(self, cmd):
        op = cmd.get("op")
        if op == "stop":
            if self.bpcam is not None:
                self.bpcam.stop()
                self.bpcam = None
            return self.reply(cmd, True, "stopped")
        if op == "start":
            if self.busy():
                return self.reply(cmd, False, "A scan is running. Wait for it to finish.")
            if self.bpcam is None or not self.bpcam.is_alive():
                cam = self.cam_module()
                self.bpcam = BpCamCalibrator(self, cam)
                self.bpcam.start()
            return self.reply(cmd, True, "started", **self.bpcam.describe())
        cal = self.bpcam
        if cal is None or not cal.is_alive():
            return self.reply(cmd, False, "the camera is not open - go back and open this screen again")
        cal.touch()
        if op == "keepalive":
            return self.reply(cmd, True, "ok")
        if op == "set":
            problem = cal.apply(cmd.get("settings") or {})
            return self.reply(cmd, problem is None, problem or "ok", **cal.describe())
        if op == "reset":
            cal.reset()
            return self.reply(cmd, True, "back to the saved settings", **cal.describe())
        if op == "light":
            cal.want_light = bool(cmd.get("on"))
            return self.reply(cmd, True, "ok", **cal.describe())
        if op == "save":
            if not all(cal.work["rois"].get(f) for f in cal.cam.FIELDS):
                return self.reply(cmd, False, "draw all three boxes (SYS, DIA, PUL) first")
            cal.save()
            return self.reply(cmd, True, "saved", **cal.describe())
        return self.reply(cmd, False, f"unknown camera op {op!r}")


class BpCamCalibrator(threading.Thread):
    """Opens the BP camera, reads the three boxes with the working settings,
    and streams a preview + the readings to the Developer options screen."""

    def __init__(self, dev, cam):
        super().__init__(daemon=True, name="bpcam-calibrate")
        self.dev, self.cam = dev, cam
        self.work = copy.deepcopy(cam.settings)        # what the screen is changing
        self.stopping = threading.Event()
        self.last_touch = time.time()
        self.want_light = True
        self.light_on = False
        self.lights = None
        self.frame_size = None
        self.problem = None
        self.readings = {}

    # -- called from the MQTT thread --
    def touch(self):
        self.last_touch = time.time()

    def stop(self):
        self.stopping.set()

    def describe(self):
        cam = self.cam
        return {"settings": self.work, "saved": cam.settings, "sliders": cam.SLIDERS,
                "fields": cam.FIELDS, "frame": self.frame_size, "light": self.light_on,
                "problem": self.problem}

    def apply(self, changes):
        cam = self.cam
        new = copy.deepcopy(self.work)
        for key, v in changes.items():
            if key == "rois":
                if not isinstance(v, dict):
                    return "rois must be {SYS: [x, y, w, h], ...}"
                for field, box in v.items():
                    if field not in cam.FIELDS:
                        return f"unknown box {field}"
                    if box is None:
                        new["rois"][field] = None
                        continue
                    if not (isinstance(box, list) and len(box) == 4
                            and all(isinstance(n, (int, float)) for n in box)):
                        return f"{field} box must be [x, y, w, h]"
                    x, y, w, h = (int(round(n)) for n in box)
                    if w < 10 or h < 10 or x < 0 or y < 0:
                        return f"{field} box is too small - drag a bigger box"
                    if self.frame_size:
                        W, H = self.frame_size
                        x, y = min(x, W - 10), min(y, H - 10)
                        w, h = min(w, W - x), min(h, H - y)
                    new["rois"][field] = [x, y, w, h]
            elif key in cam.SLIDERS:
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    return f"{key} must be a number"
                new[key] = int(max(0, min(cam.SLIDERS[key], round(v))))
            else:
                return f"{key} can't be changed here"
        self.work = new
        return None

    def reset(self):
        self.work = copy.deepcopy(self.cam.settings)

    def save(self):
        m, cam = self.dev.m, self.cam
        path = os.path.join(m.HW_DIR, cam.SETTINGS_FILE)
        if os.path.exists(path):                       # keep the previous one, like before
            shutil.copyfile(path, os.path.join(m.HW_DIR, "bp_cam_settings.backup.json"))
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.work, f, indent=2)
        os.replace(tmp, path)
        cam.settings.clear()                           # the next BP scan uses it right away
        cam.settings.update(copy.deepcopy(self.work))
        self.dev.log(f"developer options: BP camera settings saved to {path}")

    # -- the camera thread --
    def _set_light(self, on):
        if self.lights is None or on == self.light_on:
            return
        try:
            self.lights.set(on)
            self.light_on = on
        except Exception as e:
            self.dev.log(f"BP camera light: {e}")

    def run(self):
        cam, dev = self.cam, self.dev
        cv2 = cam.cv2
        cap = None
        try:
            if not dev.simulate and dev.m.ON_PI:
                try:
                    self.lights = cam.Lights("pi")
                except BaseException as e:             # Lights() raises SystemExit on problems
                    dev.log(f"BP camera light not available: {e}")
                    self.lights = None
                self._set_light(True)
                time.sleep(cam.LIGHT_WARMUP_SEC)       # light first, like bp_camTEST.py
            index = cam.default_cam()
            try:
                cap = cam.open_camera(index, cam.FRAME_W, cam.FRAME_H)
            except SystemExit as e:                    # bp_camTEST raises SystemExit if it can't open
                raise RuntimeError(str(e))
            cam.apply_exposure(cap, self.work["exposure"])
            exposure = self.work["exposure"]
            history = {f: deque(maxlen=STABLE_FRAMES) for f in cam.FIELDS}
            last_pub = 0.0
            dev.log(f"developer options: BP camera {index} open for calibration")
            while not self.stopping.is_set():
                if time.time() - self.last_touch > BPCAM_IDLE_STOP_S:
                    dev.log("developer options: BP camera closed (screen gone)")
                    break
                self._set_light(self.want_light)
                if self.work["exposure"] != exposure:
                    exposure = self.work["exposure"]
                    cam.apply_exposure(cap, exposure)
                ok, frame = cap.read()
                if not ok or frame is None:
                    self.problem = "no picture from the BP camera"
                    time.sleep(0.2)
                    continue
                self.problem = None
                H, W = frame.shape[:2]
                self.frame_size = [W, H]
                s = self.work
                readings = {}
                for field in cam.FIELDS:
                    r = s["rois"].get(field)
                    if not r:
                        history[field].clear()
                        readings[field] = {"text": None, "steady": False}
                        continue
                    x, y, w, h = r
                    x, y = max(0, min(x, W - 2)), max(0, min(y, H - 2))
                    w, h = max(2, min(w, W - x)), max(2, min(h, H - y))
                    _, binary = cam.process(frame[y:y + h, x:x + w], s)
                    text, _, _ = cam.read_number(binary, s["seg_on"] / 100.0)
                    history[field].append(text)
                    steady = (len(history[field]) == STABLE_FRAMES and bool(text)
                              and "?" not in text and len(set(history[field])) == 1)
                    readings[field] = {"text": text, "steady": steady}
                self.readings = readings
                if time.time() - last_pub >= BPCAM_PUBLISH_EVERY_S:
                    last_pub = time.time()
                    small = cv2.resize(frame, (BPCAM_PREVIEW_W, int(H * BPCAM_PREVIEW_W / W)))
                    ok_jpg, jpg = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, BPCAM_JPEG_QUALITY])
                    if ok_jpg:
                        dev.agent.publish(dev.t_preview, jpg.tobytes(), qos=0, raw=True)
                    dev.agent.publish(dev.t_dev, {"kind": "bpcam", "readings": readings,
                                                  "frame": self.frame_size, "light": self.light_on,
                                                  "ts": round(time.time(), 3)})
        except Exception as e:
            self.problem = f"BP camera problem: {e}"
            dev.log(f"developer options: {self.problem}")
            dev.agent.publish(dev.t_dev, {"kind": "bpcam", "problem": self.problem, "ts": round(time.time(), 3)})
        finally:
            if cap is not None:
                cap.release()
            if self.lights is not None:
                try:
                    self.lights.close()                # light off
                except BaseException:
                    pass
            self.light_on = False
