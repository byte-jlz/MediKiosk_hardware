"""Loads the trained model once and classifies a set of five vitals."""
import json, sys
from pathlib import Path
import joblib, numpy as np

VITAL_TO_FEATURE = {"spo2": "Oxygen_Saturation", "systolic": "Systolic_BP", "heart_rate": "Heart_Rate",
                    "temperature": "Temperature", "respiratory_rate": "Respiratory_Rate"}


class ReadingRejected(ValueError):
    """A value is missing or physically impossible: ask the patient to measure again."""
    def __init__(self, vital, value):
        super().__init__(f"{vital} reading rejected: {value}")
        self.vital, self.value = vital, value


class Classifier:
    def __init__(self, cfg):
        base = Path(cfg.get("base_dir", "."))
        sys.path.insert(0, str(base.resolve()))                 # lets joblib import features.py
        self.model = joblib.load(base / cfg["model_path"])
        mc = json.loads((base / cfg["model_config_path"]).read_text())
        self.features, self.classes = mc["features"], mc["classes"]
        self.feature_to_vital = {f: v for v, f in VITAL_TO_FEATURE.items()}
        self.hard, self.train = cfg["hard_limits"], cfg["training_range"]
        self.critical = cfg["critical_values"]

    def classify(self, vitals):
        for v in VITAL_TO_FEATURE:                                # 1. reject missing / impossible values
            val = vitals.get(v)
            lo, hi = self.hard[v]
            if val is None or not (lo <= val <= hi):
                raise ReadingRejected(v, val)
        x = np.array([[vitals[self.feature_to_vital[f]] for f in self.features]], dtype=float)  # model's order
        p = self.model.predict_proba(x)[0]
        k = int(self.model.classes_[int(np.argmax(p))])
        outside = [v for v, (lo, hi) in self.train.items() if not (lo <= vitals[v] <= hi)]
        flags = []
        for v, rule in self.critical.items():
            if "lt" in rule and vitals[v] < rule["lt"]: flags.append(f"{v}<{rule['lt']}")
            if "gt" in rule and vitals[v] > rule["gt"]: flags.append(f"{v}>{rule['gt']}")
        return {"tier": self.classes[k], "tier_rank": k,
                "probabilities": {self.classes[int(c)]: round(float(q), 4) for c, q in zip(self.model.classes_, p)},
                "in_model_range": not outside, "outside_range": outside, "critical_flags": flags}
