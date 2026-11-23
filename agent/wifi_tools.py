"""
wifi_tools.py - Wi-Fi for the kiosk's Developer options (developer PIN only).

On the Pi this uses NetworkManager's `nmcli` (Raspberry Pi OS Bookworm). The
kiosk user is in the `netdev` group, so no sudo is needed for scanning and
connecting. On the laptop / with SIMULATE=1 it pretends, with a fake list.

The password is passed to nmcli once and is never written to the agent log or
sent back to the screen. NetworkManager stores it in its own connection file
(/etc/NetworkManager/system-connections/, root only), like any saved Wi-Fi.
"""

import json
import os
import socket
import sqlite3
import subprocess
import time

NMCLI_TIMEOUT_S = 20
CONNECT_TIMEOUT_S = 45
INTERNET_HOST = ("github.com", 443)     # also what the update check needs
CHECK_TIMEOUT_S = 3
SYNC_ENV = "/etc/medikiosk/sync.env"     # the cloud broker settings (only host + port are read)


def _tcp_ok(host, port, timeout=CHECK_TIMEOUT_S):
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def _local_ip():
    """This computer's address on the network (no traffic is sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def _split(line):
    """nmcli -t output: fields separated by ':', with '\\:' for a ':' inside a field."""
    out, cur, i = [], "", 0
    while i < len(line):
        c = line[i]
        if c == "\\" and i + 1 < len(line):
            cur += line[i + 1]
            i += 2
            continue
        if c == ":":
            out.append(cur)
            cur = ""
        else:
            cur += c
        i += 1
    out.append(cur)
    return out


class Wifi:
    def __init__(self, simulate, on_pi, repo_dir, log):
        self.fake = simulate or not on_pi
        self.repo_dir = repo_dir
        self.log = log
        self.sim_current = "HealthCenter-WiFi"

    # ---------------- helpers ----------------
    def _nmcli(self, *args, timeout=NMCLI_TIMEOUT_S):
        r = subprocess.run(["nmcli", *args], capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip() or f"nmcli exit code {r.returncode}")
        return r.stdout

    def _broker(self):
        """(host, port) of the cloud broker from /etc/medikiosk/sync.env - never the password."""
        host = port = None
        try:
            with open(SYNC_ENV, encoding="utf-8") as f:
                for line in f:
                    key, _, value = line.strip().partition("=")
                    if key == "MEDIKIOSK_BROKER_HOST":
                        host = value.strip()
                    elif key == "MEDIKIOSK_BROKER_PORT":
                        port = value.strip()
        except OSError:
            return None
        return (host, port or "8883") if host else None

    def _waiting_visits(self):
        """Check-ins saved on the Pi but not yet sent to the staff console (edge outbox)."""
        try:
            with open(os.path.join(self.repo_dir, "edge", "edge_config.json"), encoding="utf-8") as f:
                db_path = json.load(f).get("db_path")
            if not db_path or not os.path.exists(db_path):
                return None
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
            try:
                row = conn.execute("SELECT COUNT(*) FROM outbox WHERE status IN ('pending', 'sending')").fetchone()
            finally:
                conn.close()
            return int(row[0])
        except (OSError, ValueError, sqlite3.Error):
            return None

    # ---------------- status ----------------
    def status(self):
        """Connected network, IP, internet, cloud broker, waiting visits. Takes a few seconds."""
        if self.fake:
            return {"simulated": True, "ssid": self.sim_current, "ip": "192.168.1.24",
                    "internet": True, "broker": True, "waiting": 0}
        ssid = None
        try:
            for line in self._nmcli("-t", "-f", "ACTIVE,SSID", "dev", "wifi").splitlines():
                active, name = (_split(line) + ["", ""])[:2]
                if active == "yes":
                    ssid = name
                    break
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
            self.log(f"wifi status: {e}")
        broker = self._broker()
        return {
            "simulated": False,
            "ssid": ssid,
            "ip": _local_ip(),
            "internet": _tcp_ok(*INTERNET_HOST),
            "broker": _tcp_ok(*broker) if broker else None,      # None = not set up on this kiosk
            "waiting": self._waiting_visits(),
        }

    # ---------------- scan ----------------
    def scan(self):
        """Networks nearby, strongest first: [{ssid, signal, secured, in_use, saved}]."""
        if self.fake:
            time.sleep(1.0)
            nets = [("HealthCenter-WiFi", 82, True), ("Globe_At_Home_5G", 74, True),
                    ("PLDT_HOME_FIBR", 55, True), ("Barangay-Free-WiFi", 30, False)]
            return [{"ssid": s, "signal": sig, "secured": sec, "in_use": s == self.sim_current,
                     "saved": s in ("HealthCenter-WiFi", self.sim_current)} for s, sig, sec in nets]
        saved = self._saved()
        out = self._nmcli("-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "dev", "wifi", "list", "--rescan", "yes",
                          timeout=NMCLI_TIMEOUT_S + 10)
        best = {}
        for line in out.splitlines():
            in_use, ssid, signal, security = (_split(line) + ["", "", "", ""])[:4]
            if not ssid:
                continue                                  # hidden network
            try:
                sig = int(signal)
            except ValueError:
                sig = 0
            net = {"ssid": ssid, "signal": sig, "secured": security not in ("", "--"),
                   "in_use": in_use.strip() == "*", "saved": ssid in saved}
            old = best.get(ssid)                          # same name on 2.4 + 5 GHz: keep the best one
            if old is None or net["in_use"] or (not old["in_use"] and sig > old["signal"]):
                best[ssid] = net
        return sorted(best.values(), key=lambda n: (not n["in_use"], -n["signal"]))

    def _saved(self):
        names = set()
        try:
            for line in self._nmcli("-t", "-f", "NAME,TYPE", "con", "show").splitlines():
                name, ctype = (_split(line) + ["", ""])[:2]
                if ctype == "802-11-wireless":
                    names.add(name)
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
            self.log(f"wifi saved list: {e}")
        return names

    # ---------------- connect ----------------
    def connect(self, ssid, password):
        """Connect (saved network: no password needed). Returns (ok, message).
        If it fails, NetworkManager goes back to a saved network by itself."""
        if self.fake:
            time.sleep(2.0)
            if password == "wrongpass":
                return False, "Wrong password (simulated: \"wrongpass\" always fails)."
            self.sim_current = ssid
            return True, f"Connected to {ssid} (simulated)."
        saved = self._saved()
        try:
            if ssid in saved and not password:
                self._nmcli("con", "up", "id", ssid, timeout=CONNECT_TIMEOUT_S)
            else:
                args = ["dev", "wifi", "connect", ssid]
                if password:
                    args += ["password", password]
                self._nmcli(*args, timeout=CONNECT_TIMEOUT_S)
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
            # careful: a TimeoutExpired's text contains the command line, password included - never show it
            timed_out = isinstance(e, subprocess.TimeoutExpired)
            text = "" if timed_out else str(e)
            if ssid not in saved:                         # don't keep a half-made profile with a wrong password
                try:
                    self._nmcli("con", "delete", "id", ssid)
                except (RuntimeError, OSError, subprocess.TimeoutExpired):
                    pass
            if timed_out:
                return False, "No answer from the network within 45 s."
            if "Secrets were required" in text or "password" in text.lower():
                return False, "Wrong password, or the network refused it."
            return False, f"Could not connect: {text[:160]}"
        return True, f"Connected to {ssid}."
