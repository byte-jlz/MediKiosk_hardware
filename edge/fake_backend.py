"""Throw-away stand-in for the real backend. Run it on your laptop while building the Pi side.
It stores each message once (by message_id), replies with an ack, and logs the order of arrival."""
import json, sqlite3, sys
import paho.mqtt.client as mqtt

DB = sqlite3.connect(sys.argv[2] if len(sys.argv) > 2 else "received.db", check_same_thread=False)
DB.execute("""CREATE TABLE IF NOT EXISTS got(seq INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT UNIQUE,
              urgency TEXT, payload TEXT)""")
DB.execute("CREATE TABLE IF NOT EXISTS arrivals(seq INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT, urgency TEXT, duplicate INTEGER)")
DB.commit()


def on_message(client, userdata, msg):
    d = json.loads(msg.payload)
    try:
        DB.execute("INSERT INTO got(message_id, urgency, payload) VALUES(?,?,?)", (d["message_id"], d["urgency"], msg.payload.decode()))
        dup = 0
    except sqlite3.IntegrityError:
        dup = 1
    DB.execute("INSERT INTO arrivals(message_id, urgency, duplicate) VALUES(?,?,?)", (d["message_id"], d["urgency"], dup))
    DB.commit()
    client.publish(f"medikiosk/{d['kiosk_id']}/ack",
                   json.dumps({"message_id": d["message_id"], "status": "duplicate" if dup else "stored"}), qos=1)


c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="fake-backend", clean_session=False)
c.on_message = on_message
c.on_connect = lambda cl, u, f, rc, p: cl.subscribe("medikiosk/+/vitals/#", qos=1)
c.connect(sys.argv[1] if len(sys.argv) > 1 else "localhost", int(sys.argv[3]) if len(sys.argv) > 3 else 1883, keepalive=30)
c.loop_forever()
