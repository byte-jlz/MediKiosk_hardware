"""Store-and-forward worker: sends outbox rows to the MQTT broker, highest tier first,
and marks a row 'synced' only when the backend's application-level ack arrives.

The broker is the CLOUD broker (EMQX Cloud when deployed). Its settings come from
edge_config.json -> "broker", and any of them can be overridden by environment
variables, so the real password never has to be committed to git:

    MEDIKIOSK_BROKER_HOST      e.g. xxxx.ala.ap-southeast-1.emqxsl.com
    MEDIKIOSK_BROKER_PORT      8883 for EMQX (TLS)
    MEDIKIOSK_BROKER_USERNAME  this kiosk's login, e.g. K-01
    MEDIKIOSK_BROKER_PASSWORD
    MEDIKIOSK_BROKER_TLS       1 / 0 (default: on when the port is 8883)
    MEDIKIOSK_BROKER_CA_FILE   only for a self-signed broker; EMQX Cloud needs none

On the Pi the service reads them from /etc/medikiosk/sync.env (see sync.env.example).
"""
import json, os, queue, random, signal, ssl, sys, threading, time
import paho.mqtt.client as mqtt
import edge_db


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} [sync] {msg}", flush=True)


def broker_settings(cfg):
    """edge_config.json's "broker", with MEDIKIOSK_BROKER_* environment variables on top."""
    b = dict(cfg.get("broker") or {})
    env = lambda k: os.environ.get(f"MEDIKIOSK_BROKER_{k}")
    if env("HOST"): b["host"] = env("HOST")
    if env("PORT"): b["port"] = int(env("PORT"))
    if env("USERNAME") is not None: b["username"] = env("USERNAME")
    if env("PASSWORD") is not None: b["password"] = env("PASSWORD")
    if env("CA_FILE") is not None: b["ca_file"] = env("CA_FILE") or None
    b.setdefault("host", "localhost")
    b["port"] = int(b.get("port") or 1883)
    if env("TLS") is not None:
        b["tls"] = env("TLS").strip().lower() in ("1", "true", "yes", "on")
    elif "tls" not in b:
        b["tls"] = b["port"] == 8883
    return b


class SyncWorker:
    def __init__(self, cfg):
        self.cfg = cfg
        self.kiosk = cfg["kiosk_id"]
        self.ack_topic = f"medikiosk/{self.kiosk}/ack"
        self.status_topic = f"medikiosk/{self.kiosk}/uplink"      # NOT /status: the kiosk agent owns that one
        self.conn = edge_db.connect(cfg["db_path"])            # used by the main thread only
        edge_db.recover(self.conn)
        self.connected = threading.Event()
        self.acks = queue.Queue()
        self.stop = False

        b = self.broker = broker_settings(cfg)
        # fixed client_id + clean_session=False: the broker remembers this kiosk's subscription
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"{self.kiosk}-sync", clean_session=False)
        if b.get("username"):
            self.client.username_pw_set(b["username"], b.get("password"))
        if b.get("tls"):
            self.client.tls_set(ca_certs=b.get("ca_file"), tls_version=ssl.PROTOCOL_TLS_CLIENT)
        self.client.will_set(self.status_topic, "offline", qos=1, retain=True)
        self.client.reconnect_delay_set(min_delay=1, max_delay=60)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.on_connect_fail = lambda *_: log(
            f"could not reach {self.broker['host']}:{self.broker['port']} - check the address, the port and TLS; retrying...")

    # --- network callbacks run in paho's thread: they never touch SQLite ---
    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:                 # e.g. "Not authorized" = wrong username / password
            log(f"broker refused the connection: {reason_code}")
            return
        client.subscribe(self.ack_topic, qos=1)
        client.publish(self.status_topic, "online", qos=1, retain=True)
        self.connected.set()
        log(f"connected to {self.broker['host']}:{self.broker['port']} - waiting for acks on {self.ack_topic}")

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        if self.connected.is_set() and not self.stop:
            log(f"disconnected ({reason_code}) - visits stay in SQLite until the broker is back")
        self.connected.clear()

    def _on_message(self, client, userdata, msg):
        try:
            self.acks.put(json.loads(msg.payload)["message_id"])
        except (ValueError, KeyError):
            pass

    # --- main loop ---
    def _wait_for_ack(self, message_id, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                got = self.acks.get(timeout=max(0.05, deadline - time.monotonic()))
            except queue.Empty:
                break
            edge_db.mark_synced(self.conn, got)                # any ack (even a late one) is good news
            if got == message_id:
                return True
        return False

    def run(self):
        b = self.broker
        log(f"connecting to {b['host']}:{b['port']}{' (TLS)' if b['tls'] else ''} as {b.get('username') or 'anonymous'}...")
        self.client.connect_async(b["host"], b["port"], keepalive=30)
        self.client.loop_start()
        last_purge = 0
        while not self.stop:
            if time.monotonic() - last_purge > 3600:
                edge_db.purge(self.conn, self.cfg.get("retention_days", 7)); last_purge = time.monotonic()
            if not self.connected.wait(timeout=1):
                continue
            row = edge_db.claim_next(self.conn)
            if row is None:
                time.sleep(0.3); continue
            info = self.client.publish(row["topic"], row["payload"], qos=1)
            ok = info.rc == mqtt.MQTT_ERR_SUCCESS and self._wait_for_ack(row["message_id"], self.cfg.get("ack_timeout_s", 10))
            if not ok:
                delay = min(2 ** row["attempts"], 60) + random.uniform(0, 1)   # exponential backoff + jitter
                edge_db.mark_retry(self.conn, row["message_id"], delay)
        self.client.loop_stop(); self.client.disconnect()


def main(cfg_path="edge_config.json"):
    worker = SyncWorker(__import__("json").load(open(cfg_path)))
    signal.signal(signal.SIGTERM, lambda *_: setattr(worker, "stop", True))
    signal.signal(signal.SIGINT, lambda *_: setattr(worker, "stop", True))
    worker.run()


if __name__ == "__main__":
    main(*sys.argv[1:])
