# Edge changes (merged into this repo on 2026-11-27)

Your newer code is kept as it was (real-kiosk guard `REAL_KIOSKS`, guest scans ignored, `patient_ref`, auto-update, etc.).
The triage framework described in the paper was added on top.

## Model (important)
- `medikiosk_model.joblib` / `medikiosk_config.json` replaced with the paper's final model (`logreg-squares`): logistic regression with squared terms,
  C = 100, class_weight = None, trained on all 800 development records. This is the model the paper evaluates
  (locked test set: accuracy 96.1%, macro recall 96.6%, High 12/12). It gives the same probabilities as the notebook's final model
  on all 1,000 records, and it was saved with scikit-learn 1.8.0 (same as the Pi).
- The previous model file (class_weight = balanced, recorded as `logreg-squares-v1`). It disagrees with the paper's model on 3.3% of records.
  The field-test check-ins from Oct 30 to Nov 13 were classified by that previous model.
- `edge_config*.json`: `model_version` -> `logreg-squares`, `training_range` updated to the training data of the paper's model.

## Triage rules (new files)
- `triage_config.json`: the one place for every triage number.
  - `gate` 0.80: below it a result gets a "Low model confidence" flag (display only).
  - `critical_limits`: NEWS2 extreme bands, with the high-BP limit from the 2024 Philippine guideline (systolic >= 180 or
    diastolic >= 120). Display only; never changes the level. Critical vital + Normal/Low level adds a "check" flag.
  - `tie_break` "probability" + `max_bypass` 2: staff-queue order inside a level is higher probability first, but a patient
    passed by 2 newer patients of the same level goes next (bounded waiting, no starvation). Never crosses levels.
- `triage_rules.py`: reads the config (flags, critical check, queue order). `test_triage.py`: 43 checks.
- `field_log.py` + `field_test_staff_form.csv`: field-test logging (`field_log`, `staff_feedback`, `queue_snapshots` tables)
  and `summary` / `export` commands.

## Changed files
- `classifier.py`: critical flags come from `triage_rules` (replaces the old placeholder `critical_values` in edge_config);
  also returns the raw probabilities and the confidence.
- `visit_recorder.py`: adds the decision (alert_level, confidence, check) to the payload and writes the field log. Priority is still the level.
- `edge_db.py`: optional extra payload fields; `mark_retake` helper.
- `schema.sql`: new tables `retake_marks`, `field_log`, `staff_feedback`, `queue_snapshots` (CREATE IF NOT EXISTS, safe on an existing database).
- `edge_service.py`: log line shows the alert level and confidence. Nothing else changed.
- `selftest.py`: critical flag name follows the new limits (`spo2<=91`).
- `README.md`: new section "Triage rules and field-test logging".

## Unchanged
- The outbox order: highest level first, then oldest first (`edge_db.claim_next`).
- Everything outside `edge/`.

## Before the defense, on the Pi
    cd edge && python3 test_triage.py && python3 test_visit_recorder.py
Both must pass (43/43 and 14/14 here).
