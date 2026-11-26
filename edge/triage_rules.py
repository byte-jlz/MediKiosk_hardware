"""Triage rules that sit on top of the model: confidence flag, critical-vital flag and queue order.
Every number comes from triage_config.json (one place, labelled placeholder until the health center / validator sign off).
The model decides the TIER and the tier alone sets the queue and transmission order. Confidence and critical vitals only raise display flags."""
import json
import math
from pathlib import Path

CLASSES = ["Normal", "Low", "Medium", "High"]
VITALS = ["spo2", "systolic", "heart_rate", "temperature", "respiratory_rate"]
OK, LOW_CONFIDENCE = "OK", "LOW_CONFIDENCE"


def load_config(path=None):
    path = Path(path) if path else Path(__file__).with_name("triage_config.json")
    cfg = json.loads(path.read_text())
    if cfg.get("tie_break") not in ("probability", "wait"):
        raise ValueError("triage_config.json: tie_break must be 'probability' or 'wait'")
    return cfg


def critical_flags(vitals, limits):
    """List of single vitals at or beyond a critical limit, e.g. ['spo2<=91']. Independent of the model."""
    flags = []
    for v in VITALS + ["diastolic"]:                       # diastolic is checked for the flag only; the model does not use it
        lim, x = limits.get(v) or {}, vitals.get(v)
        if x is None:
            continue
        if lim.get("low") is not None and x <= lim["low"]:
            flags.append(f"{v}<={lim['low']}")
        if lim.get("high") is not None and x >= lim["high"]:
            flags.append(f"{v}>={lim['high']}")
    return flags


def decide(proba, vitals, cfg):
    """proba: four probabilities in CLASSES order. Returns display flags only. Nothing here changes the tier, the queue position
    or the transmission order.
      alert_level OK / LOW_CONFIDENCE : LOW_CONFIDENCE when the top probability is below the gate (the model is less certain of its own answer)
      critical_flags          : single vitals at or beyond a limit (independent of the model)
      check                   : the model says Normal or Low while a vital is critical, so staff should look at the numbers"""
    conf = max(proba)
    tier = max(range(4), key=lambda i: proba[i])
    flags = critical_flags(vitals, cfg["critical_limits"])
    unsure = conf < cfg["gate"]
    return {"confidence": conf, "tier_rank": tier, "alert_level": LOW_CONFIDENCE if unsure else OK,
            "reason": "confidence below the gate" if unsure else "confidence at or above the gate",
            "critical_flags": flags, "check": bool(flags) and tier <= 1}


def band(p, step):
    """Round DOWN to steps so near-equal probabilities fall through to waiting time. step 0 = no rounding (strict)."""
    return p if step <= 0 else math.floor(p / step + 1e-9) * step


def priority_key(rec, tie_break, step=0.0, critical_first=False):
    """Smaller sorts first. rec: tier_rank, proba (CLASSES order), wait_min, critical (bool). Tier always comes first.
    critical_first (off by default): inside a tier, a record with a critical vital goes before the others. Never across tiers."""
    t = rec["tier_rank"]
    crit = 0 if (critical_first and rec.get("critical")) else 1
    if tie_break == "wait":
        return (-t, crit, -rec["wait_min"])
    return (-t, crit, -band(rec["proba"][t], step), -rec["wait_min"])


def _bounded(tier_recs, key, max_bypass):
    """Bounded waiting inside one tier: follow `key` (probability order), but a record that has already been passed by
    `max_bypass` newer records of the same tier goes next (oldest such record first). A record's optional "bypassed"
    field carries passes that happened earlier (newer same-tier patients already served while it waited)."""
    remaining = sorted(tier_recs, key=key)
    passed = {id(r): int(r.get("bypassed", 0)) for r in remaining}
    out = []
    while remaining:
        due = [r for r in remaining if passed[id(r)] >= max_bypass]
        pick = max(due, key=lambda r: r["wait_min"]) if due else remaining[0]
        remaining.remove(pick)
        out.append(pick)
        for r in remaining:                       # everyone older than `pick` was just passed by a newer record
            if r["wait_min"] > pick["wait_min"]:
                passed[id(r)] += 1
    return out


def queue_order(records, cfg, tie_break=None, critical_first=None):
    """Tier first, always. Inside a tier: 'probability' (default) = higher probability of the tier first, with bounded
    waiting (cfg max_bypass, default 2) so no record can be passed over indefinitely; 'wait' = oldest first."""
    tb = tie_break or cfg["tie_break"]
    cf = bool(cfg.get("critical_first", False)) if critical_first is None else critical_first
    key = lambda r: priority_key(r, tb, cfg.get("prob_step", 0.0), cf)
    ordered = sorted(records, key=key)
    k = cfg.get("max_bypass")
    if tb != "probability" or k is None:
        return ordered
    out = []
    for t in sorted({r["tier_rank"] for r in ordered}, reverse=True):
        out += _bounded([r for r in ordered if r["tier_rank"] == t], key, int(k))
    return out


def queue_both(records, cfg):
    """Order under both rules, so the field test can count how often they disagree. Every record is ranked by tier first;
    flags (low model confidence, critical) never move a record."""
    ids = lambda tb: [r["id"] for r in queue_order(records, cfg, tb)]
    prob, wait = ids("probability"), ids("wait")
    cf_on = [r["id"] for r in queue_order(records, cfg, None, True)]
    cf_off = [r["id"] for r in queue_order(records, cfg, None, False)]
    return {"active": cfg["tie_break"], "probability": prob, "wait": wait, "differs": prob != wait, "n_queue": len(records), "critical_first": bool(cfg.get("critical_first", False)),
            "order_critical_first": cf_on, "differs_critical": cf_on != cf_off,
            "n_low_confidence": sum(1 for r in records if r.get("alert_level") == LOW_CONFIDENCE)}
