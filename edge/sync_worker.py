"""Store-and-forward worker: sends outbox rows to the MQTT broker, highest tier first,
and marks a row 'synced' only when the backend's application-level ack arrives."""
import json, queue, random, signal, ssl, sys, threading, time
import paho.mqtt.client as mqtt
import edge_db


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

        b = cfg["broker"]
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

    # --- network callbacks run in paho's thread: they never touch SQLite ---
    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if not reason_code.is_failure:
            client.subscribe(self.ack_topic, qos=1)
            client.publish(self.status_topic, "online", qos=1, retain=True)
            self.connected.set()

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
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
        b = self.cfg["broker"]
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
