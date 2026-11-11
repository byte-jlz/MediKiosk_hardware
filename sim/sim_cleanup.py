"""
sim_cleanup.py - delete everything the LAPTOP SIMULATION left behind.

    1. on this laptop: the simulation folder (edge database, backup CSVs, logs)
    2. in MongoDB:     the simulated visits (kiosk "K-SIM" only)

The real kiosk (K-01 on the Pi) is never touched: this script only ever deletes
data whose kiosk is K-SIM, and refuses to run for any other kiosk ID.

What it does NOT delete, on purpose:
    - audit_logs: append-only by design (who logged in when) - deleting them would
      break the audit trail. Simulated QR logins stay there as normal logins.
    - patients, staff accounts, the kiosk registration of K-SIM (so the next
      simulation works without registering again).

Usage (start_sim.ps1 runs this for you when the simulation ends):
    python sim\\sim_cleanup.py                  # shows what would be deleted, then asks
    python sim\\sim_cleanup.py --yes            # no question
    python sim\\sim_cleanup.py --local-only     # only the laptop folder, not MongoDB

MongoDB needs MONGO_URI (and MONGO_DB, default db-test) in the environment - type
them in the PowerShell window only, never save them in a file in the repo.
"""

import argparse
import os
import shutil
import sys
from pathlib import Path

SIM_KIOSK = "K-SIM"
SIM_DIR = Path(os.getenv("MEDIKIOSK_SIM_DIR", str(Path.home() / "medikiosk-sim")))
# the collections a simulated visit leaves data in (both carry kiosk_id)
CLOUD_COLLECTIONS = ("visits", "rejected_messages")


def say(msg):
    print(f"[sim-cleanup] {msg}", flush=True)


def check_kiosk(kiosk):
    # Hard guard: never delete data of a real kiosk.
    if kiosk != SIM_KIOSK:
        say(f"refusing: kiosk is {kiosk!r} - this script only cleans up {SIM_KIOSK!r}")
        sys.exit(2)


def local_cleanup(sim_dir, yes):
    sim_dir = sim_dir.resolve()
    if not sim_dir.exists():
        say(f"laptop: nothing to delete ({sim_dir} does not exist)")
        return 0
    # guard: only a folder whose name says it is the simulation's
    if "medikiosk-sim" not in sim_dir.name:
        say(f"refusing to delete {sim_dir} - its name must contain 'medikiosk-sim'")
        return 0
    files = [p for p in sim_dir.rglob("*") if p.is_file()]
    say(f"laptop: {sim_dir} - {len(files)} file(s)")
    for p in files[:10]:
        say(f"    {p.relative_to(sim_dir)}")
    if len(files) > 10:
        say(f"    ... and {len(files) - 10} more")
    if not yes and input("[sim-cleanup] delete this folder? (y/N) ").strip().lower() != "y":
        say("laptop folder kept")
        return 0
    shutil.rmtree(sim_dir, ignore_errors=True)
    say("laptop folder deleted" if not sim_dir.exists() else
        "could not delete everything - is a simulation window still open?")
    return len(files)


def cloud_cleanup(db, yes):
    counts = {c: db[c].count_documents({"kiosk_id": SIM_KIOSK}) for c in CLOUD_COLLECTIONS}
    total = sum(counts.values())
    say("MongoDB: " + ", ".join(f"{n} in {c}" for c, n in counts.items()) + f" (kiosk {SIM_KIOSK} only)")
    if total == 0:
        return 0
    if not yes and input(f"[sim-cleanup] delete these {total} simulated record(s)? (y/N) ").strip().lower() != "y":
        say("MongoDB records kept")
        return 0
    deleted = 0
    for c in CLOUD_COLLECTIONS:
        deleted += db[c].delete_many({"kiosk_id": SIM_KIOSK}).deleted_count
    left_real = db["visits"].count_documents({"kiosk_id": {"$ne": SIM_KIOSK}})
    say(f"MongoDB: deleted {deleted} simulated record(s); real visits untouched ({left_real} from other kiosks)")
    return deleted


def connect_mongo():
    uri = os.getenv("MONGO_URI")
    if not uri:
        say("MongoDB skipped: MONGO_URI is not set in this window")
        say('    to clean the cloud too:  $env:MONGO_URI="mongodb+srv://..."; $env:MONGO_DB="db-test"')
        return None
    try:
        from pymongo import MongoClient
    except ImportError:
        say("MongoDB skipped: pymongo is not installed (pip install pymongo)")
        return None
    client = MongoClient(uri, serverSelectionTimeoutMS=8000)
    client.admin.command("ping")
    return client[os.getenv("MONGO_DB", "db-test")]


def main(argv=None, db=None):
    ap = argparse.ArgumentParser(description="Delete the laptop simulation's data (kiosk K-SIM only).")
    ap.add_argument("--yes", action="store_true", help="don't ask before deleting")
    ap.add_argument("--local-only", action="store_true", help="only the laptop folder, not MongoDB")
    ap.add_argument("--kiosk", default=os.getenv("KIOSK_ID", SIM_KIOSK), help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    check_kiosk(args.kiosk)

    local_cleanup(SIM_DIR, args.yes)
    if not args.local_only:
        try:
            db = db if db is not None else connect_mongo()
            if db is not None:
                cloud_cleanup(db, args.yes)
        except Exception as e:                         # no internet / wrong password: say so, don't crash
            say(f"MongoDB cleanup failed ({e.__class__.__name__}: {str(e)[:150]}) - run it again later")
    say("done")


if __name__ == "__main__":
    main()
