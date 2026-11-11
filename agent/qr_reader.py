"""
qr_reader.py - the kiosk's QR login camera, owned by the agent.

Why here and not in the browser: the BP cam and the QR cam are the same
webcam model, so the browser sees two identical cameras and often picks the
BP one. Only the USB port tells them apart, and only Linux (not the browser)
can see that:

    hub port 1.1 (top blue)    -> BP cam   (measurements.BP_CAMERA)
    hub port 1.2 (bottom blue) -> QR cam   (measurements.QR_CAMERA)

So the agent opens the QR cam by its port path, decodes the QR code with
OpenCV, and sends the screen a small preview so the patient can aim:

    tablet --cmd-->  {"action": "qr_start", "id": "..."}   start looking
                     {"action": "qr_stop",  "id": "..."}   stop (screen left)
    tablet <--       medikiosk/<id>/qr          JSON, see agent/README.md
    tablet <--       medikiosk/<id>/qr/preview  raw JPEG bytes, ~5 per second

The QR text is sent ONLY on the qr topic - never on `result` - so the login
token doesn't end up in the backend's or edge service's scan logs.

The QR cam doesn't share anything with the sensors (I2C, scale, BP cam), so
it runs next to a scan instead of waiting in the scan queue.
"""

import os
import queue
import threading
import time

PREVIEW_WIDTH = 480        # px - small enough for MQTT, big enough to aim
PREVIEW_FPS = 5
PREVIEW_JPEG_QUALITY = 60
QR_TIMEOUT_S = float(os.getenv("QR_TIMEOUT", "120"))   # screen re-starts it if still open
FRAME_W, FRAME_H = 1280, 720


def _make_detector(cv2):
    """The newer ArUco-based detector reads phone screens better; fall back to the classic one."""
    for name in ("QRCodeDetectorAruco", "QRCodeDetector"):
        cls = getattr(cv2, name, None)
        if cls is not None:
            try:
                return cls()
            except Exception:
                continue
    return None


def open_qr_camera(cv2, camera, on_pi):
    """Open the QR cam. `camera` is a /dev/v4l/by-path/... path (Pi) or a number."""
    cam = str(camera)
    if cam.isdigit():
        index = int(cam)
    elif on_pi:
        real = os.path.realpath(cam)
        if not os.path.exists(real) or not real.startswith("/dev/video"):
            raise RuntimeError(f"QR cam not found at {cam} - is it plugged into hub port 1.2 "
                               "(bottom blue)? (check: ls -l /dev/v4l/by-path/)")
        index = int(real[len("/dev/video"):])
    else:
        index = 0                                   # laptop: the built-in webcam
    backend = getattr(cv2, "CAP_V4L2", 0) if on_pi else getattr(cv2, "CAP_ANY", 0)
    cap = cv2.VideoCapture(index, backend)
    if not cap or not cap.isOpened():
        raise RuntimeError(f"could not open the QR cam (camera {index})")
    if on_pi:
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_W)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_H)
    return cap, index


class QrReader:
    """One QR session at a time. start() replaces any running one."""

    def __init__(self, cv2, camera, on_pi, publish_event, publish_preview, log):
        self.cv2 = cv2
        self.camera = camera
        self.on_pi = on_pi
        self.publish_event = publish_event          # (dict) -> None
        self.publish_preview = publish_preview      # (bytes) -> None
        self.log = log
        self._lock = threading.Lock()
        self._thread = None
        self._stop = None
        self._id = None
        # Commands are done one by one, IN THE ORDER THEY ARRIVED, by one worker thread.
        # (The screen often sends start -> stop -> start within milliseconds when it
        # opens; doing them out of order left the camera off while the screen waited.)
        self._cmds = queue.Queue()
        threading.Thread(target=self._worker, daemon=True, name="qr-commands").start()

    # ---------------- commands (safe to call from the MQTT thread: they only queue) ----------------
    def start(self, sid):
        self._cmds.put(("start", sid))

    def stop(self, sid=None):
        self._cmds.put(("stop", sid))

    def stop_now(self):
        """For shutdown: stop whatever runs, right now."""
        self._do_stop(None)

    def _worker(self):
        while True:
            action, sid = self._cmds.get()
            try:
                if action == "start":
                    self._do_stop(None)             # one camera, one session
                    self._do_start(sid)
                else:
                    self._do_stop(sid)
            except Exception as e:                  # never let the worker die
                self.log(f"QR {action} ({sid}) failed: {e}")

    def _do_start(self, sid):
        stop = threading.Event()
        t = threading.Thread(target=self._run, args=(sid, stop), daemon=True, name="qr-reader")
        with self._lock:
            self._id, self._stop, self._thread = sid, stop, t
        t.start()

    def _do_stop(self, sid=None):
        with self._lock:
            if self._thread is None or (sid and sid != self._id):
                return False
            stop, t = self._stop, self._thread
            self._thread = self._stop = self._id = None
        stop.set()
        t.join(timeout=10)                          # wait until the camera is really closed
        if t.is_alive():
            self.log("QR camera is slow to close - next scan may wait for it")
        return True

    # ---------------- the camera loop ----------------
    def _event(self, sid, phase, **fields):
        self.publish_event({"id": sid, "phase": phase, "ts": round(time.time(), 3), **fields})

    def _run(self, sid, stop):
        cv2 = self.cv2
        if cv2 is None or not hasattr(cv2, "VideoCapture"):
            self._event(sid, "error", error="OpenCV is not installed for the agent (pip install opencv-python)")
            return
        detector = _make_detector(cv2)
        if detector is None:
            self._event(sid, "error", error="this OpenCV has no QR code detector")
            return
        try:
            cap, index = open_qr_camera(cv2, self.camera, self.on_pi)
        except Exception as e:
            self.log(f"QR camera problem: {e}")
            self._event(sid, "error", error=str(e))
            return

        self.log(f"QR scan ({sid}) started on camera {index}")
        self._event(sid, "started", camera=index)
        t_end = time.time() + QR_TIMEOUT_S
        next_preview = 0.0
        found = None
        try:
            while not stop.is_set():
                if time.time() > t_end:
                    self._event(sid, "timeout")
                    break
                ok, frame = cap.read()
                if not ok or frame is None:
                    time.sleep(0.05)
                    continue
                now = time.time()
                if now >= next_preview:
                    next_preview = now + 1.0 / PREVIEW_FPS
                    h, w = frame.shape[:2]
                    small = cv2.resize(frame, (PREVIEW_WIDTH, int(h * PREVIEW_WIDTH / w)))
                    ok_jpg, jpg = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, PREVIEW_JPEG_QUALITY])
                    if ok_jpg:
                        self.publish_preview(jpg.tobytes())
                try:
                    text, _points, _ = detector.detectAndDecode(frame)
                except cv2.error:
                    text = ""
                if text:
                    found = text
                    break
        finally:
            cap.release()

        if found and not stop.is_set():
            self.log(f"QR scan ({sid}) -> code found ({len(found)} characters)")
            self._event(sid, "found", text=found)
        elif stop.is_set():
            self.log(f"QR scan ({sid}) stopped")
