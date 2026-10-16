#!/usr/bin/env python3
"""
test_bp_camera.py - live USB webcam reader for the BP monitor's 7-segment LCD.

Draw three boxes on the camera feed: SYS (red), DIA (green), PUL (blue).
Each box is cleaned up, thresholded, and its digits are decoded into a number.

Windows:
  Raw        - live camera feed with the three boxes and decoded values.
  Processed  - per-row view: enhanced | threshold (digit boxes drawn) | value.
  Controls   - sliders to tune processing live.

Keys (click on a video window first):
  1 / 2 / 3  - choose which box to draw next (SYS / DIA / PUL), then drag
  r          - clear all boxes
  s          - save snapshot images to bp_snapshots/
  w          - write settings to bp_cam_settings.json
  q / ESC    - quit (settings are auto-saved)

Usage:
  python test_bp_camera.py            # camera 0
  python test_bp_camera.py --cam 1    # another camera
  python test_bp_camera.py --width 1280 --height 720  # other resolution
  python test_bp_camera.py --backend msmf             # Windows: same driver path as the Camera app
"""

import argparse
import json
import os
import time
from collections import deque

import cv2
import numpy as np

# ---------------- Config (tune here) ----------------
FRAME_W, FRAME_H = 1920, 1080  # requested camera resolution (full sensor view on most 1080p webcams;
                               # some webcams CROP the picture at 1280x720)
RAW_VIEW_WIDTH = 960           # Raw window width on screen
DISPLAY_SCALE = 0.5            # recalculated from the real frame size at startup
PROC_HEIGHT = 200              # each row ROI is resized to this height before processing
ROW_VIEW_HEIGHT = 140          # height of each row in the Processed window
STABLE_FRAMES = 5              # same reading N frames in a row -> "locked"
SETTINGS_FILE = "bp_cam_settings.json"
SNAPSHOT_DIR = "bp_snapshots"

FIELDS = ["SYS", "DIA", "PUL"]
COLORS = {"SYS": (0, 0, 255), "DIA": (0, 200, 0), "PUL": (255, 0, 0)}  # BGR
VALID_RANGE = {"SYS": (60, 260), "DIA": (30, 160), "PUL": (30, 220)}

DEFAULTS = {
    "rois": {"SYS": None, "DIA": None, "PUL": None},  # [x, y, w, h] each
    "channel": 0,     # 0=gray, 1=blue, 2=green, 3=red
    "exposure": 0,    # 0 = auto, >0 = manual (Linux/V4L2 values)
    "clahe": 20,      # CLAHE clip limit x10
    "blur": 2,        # Gaussian kernel = blur*2+1 (0 = off)
    "block": 50,      # adaptive threshold block = block*2+1
    "c": 10,          # adaptive threshold constant (raise to hide ghost segments)
    "close": 3,       # morph close kernel = close*2+1 (0 = off)
    "shear": 40,      # 30 = no shear; >30 straightens right-leaning digits
    "seg_on": 35,     # % of a segment area that must be white to count as ON
}

SLIDERS = {"channel": 3, "exposure": 2000, "clahe": 80, "blur": 5, "block": 100,
           "c": 40, "close": 6, "shear": 60, "seg_on": 90}

# ---------------- Settings I/O ----------------
def load_settings():
    s = json.loads(json.dumps(DEFAULTS))
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE) as f:
                saved = json.load(f)
            saved.pop("roi", None)  # old single-box format
            if isinstance(saved.get("rois"), dict):
                s["rois"].update(saved.pop("rois"))
            s.update({k: v for k, v in saved.items() if k in DEFAULTS})
            print(f"[info] loaded settings from {SETTINGS_FILE}")
        except Exception as e:
            print(f"[warn] could not read {SETTINGS_FILE}: {e}")
    return s


def save_settings(s):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(s, f, indent=2)
    print(f"[info] settings saved to {SETTINGS_FILE}")


settings = load_settings()

# ---------------- Mouse ROI selection ----------------
state = {"target": "SYS", "start": None, "end": None, "dragging": False}


def next_empty_field(after):
    order = FIELDS[FIELDS.index(after) + 1:] + FIELDS[:FIELDS.index(after) + 1]
    for f in order:
        if not settings["rois"].get(f):
            return f
    return after


def on_mouse(event, x, y, flags, _):
    fx, fy = int(x / DISPLAY_SCALE), int(y / DISPLAY_SCALE)
    if event == cv2.EVENT_LBUTTONDOWN:
        state.update(start=(fx, fy), end=(fx, fy), dragging=True)
    elif event == cv2.EVENT_MOUSEMOVE and state["dragging"]:
        state["end"] = (fx, fy)
    elif event == cv2.EVENT_LBUTTONUP and state["dragging"]:
        state["dragging"] = False
        (x0, y0), (x1, y1) = state["start"], (fx, fy)
        x0, x1 = sorted((x0, x1))
        y0, y1 = sorted((y0, y1))
        if x1 - x0 > 15 and y1 - y0 > 15:
            t = state["target"]
            settings["rois"][t] = [x0, y0, x1 - x0, y1 - y0]
            print(f"[info] {t} box set: {settings['rois'][t]}")
            state["target"] = next_empty_field(t)


# ---------------- Camera ----------------
BACKENDS = {"auto": cv2.CAP_ANY, "dshow": cv2.CAP_DSHOW, "msmf": cv2.CAP_MSMF, "v4l2": cv2.CAP_V4L2}


def open_camera(index, width=FRAME_W, height=FRAME_H, backend=None):
    if backend is None:
        backend = "dshow" if os.name == "nt" else "v4l2"
    cap = cv2.VideoCapture(index, BACKENDS[backend])
    if not cap.isOpened():
        cap = cv2.VideoCapture(index)
    if not cap.isOpened():
        raise SystemExit(f"[error] cannot open camera {index}")
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    aw, ah = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print(f"[info] camera {index} opened with {backend} at {aw}x{ah} (asked for {width}x{height})")
    if (aw, ah) != (width, height):
        print("[warn] camera did not accept the requested size - try --width/--height or --backend")
    return cap


def apply_exposure(cap, value):
    if value == 0:
        cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 3)
    else:
        cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 1)
        cap.set(cv2.CAP_PROP_EXPOSURE, value)


# ---------------- Image processing ----------------
def process(roi_bgr, s):
    scale = PROC_HEIGHT / roi_bgr.shape[0]
    roi_bgr = cv2.resize(roi_bgr, None, fx=scale, fy=scale,
                         interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)
    g = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2GRAY) if s["channel"] == 0 \
        else roi_bgr[:, :, s["channel"] - 1].copy()
    g = cv2.createCLAHE(clipLimit=max(s["clahe"], 1) / 10.0, tileGridSize=(8, 8)).apply(g)
    k = s["blur"] * 2 + 1
    if k > 1:
        g = cv2.GaussianBlur(g, (k, k), 0)
    k_shear = (s["shear"] - 30) / 100.0
    if k_shear:
        h, w = g.shape
        M = np.float32([[1, k_shear, -k_shear * h / 2], [0, 1, 0]])
        g = cv2.warpAffine(g, M, (w, h), borderMode=cv2.BORDER_REPLICATE)
    binary = cv2.adaptiveThreshold(g, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                   cv2.THRESH_BINARY_INV, max(s["block"] * 2 + 1, 3), s["c"])
    if s["close"] > 0:
        ks = s["close"] * 2 + 1
        binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE,
                                  cv2.getStructuringElement(cv2.MORPH_RECT, (ks, ks)))
    return g, binary


# ---------------- 7-segment decoding ----------------
#            a  b  c  d  e  f  g
DIGITS = {
    (1, 1, 1, 1, 1, 1, 0): "0", (0, 1, 1, 0, 0, 0, 0): "1",
    (1, 1, 0, 1, 1, 0, 1): "2", (1, 1, 1, 1, 0, 0, 1): "3",
    (0, 1, 1, 0, 0, 1, 1): "4", (1, 0, 1, 1, 0, 1, 1): "5",
    (1, 0, 1, 1, 1, 1, 1): "6", (1, 1, 1, 0, 0, 0, 0): "7",
    (1, 1, 1, 0, 0, 1, 0): "7",  # some LCDs light 'f' on 7
    (1, 1, 1, 1, 1, 1, 1): "8", (1, 1, 1, 1, 0, 1, 1): "9",
    (1, 1, 1, 0, 0, 1, 1): "9",  # 9 without bottom bar
}
# segment areas as fractions of the digit box: (x0, x1, y0, y1)
SEG_AREAS = [
    (0.25, 0.75, 0.00, 0.18),  # a top
    (0.65, 1.00, 0.12, 0.45),  # b upper right
    (0.65, 1.00, 0.55, 0.88),  # c lower right
    (0.25, 0.75, 0.82, 1.00),  # d bottom
    (0.00, 0.35, 0.55, 0.88),  # e lower left
    (0.00, 0.35, 0.12, 0.45),  # f upper left
    (0.25, 0.75, 0.41, 0.59),  # g middle
]


def clean_blobs(binary):
    """Remove speckles, wide noise strips, and bezel/edge lines from a row."""
    H, W = binary.shape
    n, labels, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
    keep = np.zeros(n, bool)
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if area < H * W * 0.003:                       # speckle
            continue
        if w > W * 0.6:                                # strip across the row
            continue
        if h < H * 0.25 and (y == 0 or y + h >= H):    # flat junk on top/bottom edge
            continue
        if w < H * 0.2 and (x == 0 or x + w >= W):     # bezel line on side edge
            continue
        keep[i] = True
    return np.where(keep[labels], 255, 0).astype(np.uint8)


def find_digit_boxes(binary):
    """Split the row into digit boxes using the column projection."""
    H, W = binary.shape
    cols = (binary > 0).sum(axis=0) > H * 0.04
    runs, start = [], None
    for x, on in enumerate(list(cols) + [False]):
        if on and start is None:
            start = x
        elif not on and start is not None:
            runs.append([start, x])
            start = None
    # merge runs separated by tiny gaps (broken segments)
    merged = []
    for r in runs:
        if merged and r[0] - merged[-1][1] < H * 0.06:
            merged[-1][1] = r[1]
        else:
            merged.append(r)
    boxes = []
    for x0, x1 in merged:
        ys = np.where((binary[:, x0:x1] > 0).any(axis=1))[0]
        if len(ys) == 0:
            continue
        y0, y1 = ys[0], ys[-1] + 1
        boxes.append((x0, y0, x1 - x0, y1 - y0))
    if not boxes:
        return []
    digit_h = max(b[3] for b in boxes)
    # keep only tall blobs (real digits); drop noise and hairline slivers
    return [b for b in boxes
            if b[3] > digit_h * 0.6 and digit_h > H * 0.35 and b[2] > digit_h * 0.08]


def decode_digit(binary, box, digit_h, on_frac):
    x, y, w, h = box
    if w < digit_h * 0.35:           # narrow + tall = "1" if it's a solid bar
        fill = (binary[y:y + h, x:x + w] > 0).mean()
        return "1" if fill > 0.4 else ""   # sparse column = leftover noise
    # a narrow box may be a tall digit's right side only; use full height
    y0 = max(0, y + h - digit_h)
    cell = binary[y0:y0 + digit_h, x:x + w]
    ch, cw = cell.shape
    segs = []
    for (fx0, fx1, fy0, fy1) in SEG_AREAS:
        area = cell[int(fy0 * ch):max(int(fy1 * ch), int(fy0 * ch) + 1),
                    int(fx0 * cw):max(int(fx1 * cw), int(fx0 * cw) + 1)]
        segs.append(1 if area.size and (area > 0).mean() > on_frac else 0)
    segs = tuple(segs)
    if segs in DIGITS:
        return DIGITS[segs]
    # closest match, allowing one wrong segment
    best, dist = "?", 99
    for pattern, d in DIGITS.items():
        dd = sum(a != b for a, b in zip(pattern, segs))
        if dd < dist:
            best, dist = d, dd
    return best if dist <= 1 else "?"


def read_number(binary, on_frac):
    """Returns (text, digit_boxes, cleaned_binary)."""
    binary = clean_blobs(binary)
    boxes = find_digit_boxes(binary)
    if not boxes:
        return None, boxes, binary
    digit_h = max(b[3] for b in boxes)
    text = "".join(decode_digit(binary, b, digit_h, on_frac) for b in boxes)
    return text, boxes, binary


# ---------------- Display helpers ----------------
def to_row(img, h, interp):
    scale = h / img.shape[0]
    out = cv2.resize(img, (max(int(img.shape[1] * scale), 1), h), interpolation=interp)
    return out if out.ndim == 3 else cv2.cvtColor(out, cv2.COLOR_GRAY2BGR)


def value_panel(field, text, locked, h):
    panel = np.zeros((h, 230, 3), np.uint8)
    color = COLORS[field]
    cv2.putText(panel, field, (10, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.9, color, 2)
    shown = text if text else "--"
    lo, hi = VALID_RANGE[field]
    ok = text and text.isdigit() and lo <= int(text) <= hi
    cv2.putText(panel, shown, (10, h - 30), cv2.FONT_HERSHEY_SIMPLEX, 1.8,
                (255, 255, 255) if ok else (0, 165, 255), 3)
    if locked:
        cv2.putText(panel, "LOCKED", (130, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
    return panel


def save_snapshot(frame, parts):
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    cv2.imwrite(os.path.join(SNAPSHOT_DIR, f"{ts}_raw.png"), frame)
    for field, (roi, binary) in parts.items():
        cv2.imwrite(os.path.join(SNAPSHOT_DIR, f"{ts}_{field}_roi.png"), roi)
        cv2.imwrite(os.path.join(SNAPSHOT_DIR, f"{ts}_{field}_binary.png"), binary)
    print(f"[info] snapshot saved: {SNAPSHOT_DIR}/{ts}_*.png")


# ---------------- Main ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cam", type=int, default=0)
    ap.add_argument("--width", type=int, default=FRAME_W, help="try 640, 1280, 1920")
    ap.add_argument("--height", type=int, default=FRAME_H, help="try 480, 720, 1080")
    ap.add_argument("--backend", choices=list(BACKENDS), default=None,
                    help="camera driver: dshow (Windows default), msmf, v4l2 (Linux default), auto")
    args = ap.parse_args()
    cap = open_camera(args.cam, args.width, args.height, args.backend)

    # fit the Raw window to the screen whatever the camera resolution is
    global DISPLAY_SCALE
    DISPLAY_SCALE = min(1.0, RAW_VIEW_WIDTH / max(int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), 1))

    cv2.namedWindow("Raw")
    cv2.setMouseCallback("Raw", on_mouse)
    cv2.namedWindow("Processed")
    cv2.namedWindow("Controls", cv2.WINDOW_NORMAL)
    cv2.resizeWindow("Controls", 420, 360)
    for name, mx in SLIDERS.items():
        cv2.createTrackbar(name, "Controls", int(settings[name]), mx, lambda v: None)

    state["target"] = next_empty_field(FIELDS[-1])
    history = {f: deque(maxlen=STABLE_FRAMES) for f in FIELDS}
    last_locked = {}
    last_exposure = None
    fps, t_prev = 0.0, time.time()

    while True:
        ok, frame = cap.read()
        if not ok:
            time.sleep(0.2)
            continue
        for name in SLIDERS:
            settings[name] = cv2.getTrackbarPos(name, "Controls")
        if settings["exposure"] != last_exposure:
            apply_exposure(cap, settings["exposure"])
            last_exposure = settings["exposure"]

        H, W = frame.shape[:2]
        rows, parts, readings = [], {}, {}
        for field in FIELDS:
            r = settings["rois"].get(field)
            if not r:
                history[field].clear()
                continue
            x, y, w, h = r
            x, y = max(0, min(x, W - 2)), max(0, min(y, H - 2))
            w, h = max(2, min(w, W - x)), max(2, min(h, H - y))
            roi = frame[y:y + h, x:x + w].copy()
            enhanced, binary = process(roi, settings)
            text, boxes, binary = read_number(binary, settings["seg_on"] / 100.0)
            parts[field] = (roi, binary)

            history[field].append(text)
            locked = (len(history[field]) == STABLE_FRAMES and text
                      and "?" not in text and len(set(history[field])) == 1)
            readings[field] = (text, locked)
            if locked and last_locked.get(field) != text:
                last_locked[field] = text
                print(f"[read] {field} = {text}")

            bin_vis = cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR)
            for (bx, by, bw, bh) in boxes:
                cv2.rectangle(bin_vis, (bx, by), (bx + bw, by + bh), COLORS[field], 2)
            rows.append(np.hstack([
                to_row(enhanced, ROW_VIEW_HEIGHT, cv2.INTER_LINEAR),
                to_row(bin_vis, ROW_VIEW_HEIGHT, cv2.INTER_NEAREST),
                value_panel(field, text, locked, ROW_VIEW_HEIGHT),
            ]))

        if rows:
            width = max(r.shape[1] for r in rows)
            rows = [cv2.copyMakeBorder(r, 0, 4, 0, width - r.shape[1],
                                       cv2.BORDER_CONSTANT, value=(40, 40, 40)) for r in rows]
            cv2.imshow("Processed", np.vstack(rows))
        else:
            blank = np.zeros((ROW_VIEW_HEIGHT, 520, 3), np.uint8)
            cv2.putText(blank, "Drag boxes: SYS, then DIA, then PUL", (15, ROW_VIEW_HEIGHT // 2),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 255), 2)
            cv2.imshow("Processed", blank)

        # raw overlay
        disp = frame.copy()
        for field in FIELDS:
            r = settings["rois"].get(field)
            if not r:
                continue
            x, y, w, h = r
            cv2.rectangle(disp, (x, y), (x + w, y + h), COLORS[field], 3)
            text, locked = readings.get(field, (None, False))
            label = f"{field} {text}" if text else field
            cv2.putText(disp, label, (max(0, x - 10), max(30, y - 10)),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.1, COLORS[field], 3)
        if state["dragging"]:
            cv2.rectangle(disp, state["start"], state["end"], COLORS[state["target"]], 2)

        now = time.time()
        fps = 0.9 * fps + 0.1 / max(now - t_prev, 1e-6)
        t_prev = now
        cv2.putText(disp, f"FPS {fps:4.1f} | drawing: {state['target']} (1/2/3) | r=reset s=snap q=quit",
                    (10, 35), cv2.FONT_HERSHEY_SIMPLEX, 0.9, COLORS[state["target"]], 2)
        cv2.imshow("Raw", cv2.resize(disp, None, fx=DISPLAY_SCALE, fy=DISPLAY_SCALE))

        key = cv2.waitKey(1) & 0xFF
        if key in (ord("q"), 27):
            break
        elif key in (ord("1"), ord("2"), ord("3")):
            state["target"] = FIELDS[key - ord("1")]
            print(f"[info] next box: {state['target']}")
        elif key == ord("s"):
            save_snapshot(frame, parts)
        elif key == ord("w"):
            save_settings(settings)
        elif key == ord("r"):
            settings["rois"] = {f: None for f in FIELDS}
            state["target"] = "SYS"
            print("[info] boxes cleared")

    save_settings(settings)
    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()