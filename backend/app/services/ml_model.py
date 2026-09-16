import logging
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple
from collections import Counter

import numpy as np

logger = logging.getLogger(__name__)

MODEL_DOC_ID = "target_predictor_v1"
MIN_STRATEGY_SAMPLES = 20  # strategies with fewer samples get bucketed as "OTHER"
L2_REG = 0.01
LEARNING_RATE = 0.15
ITERATIONS = 2000

# ⚠️ pcr, orb, score deliberately EXCLUDED — for the vast majority of logged
# rows (everything from strategy_engine.py's shared execution path) these were
# hardcoded (pcr=0.0, score=6.0, orb=None) at logging time, so they carry
# almost no real signal. Only genuinely-variable fields are used.
NUMERIC_FIELDS = ["delta", "iv", "dir", "mom", "minutes_of_day"]

_cached_model = None
_cache_loaded_at = None
CACHE_TTL_SECONDS = 60


def _hm_to_minutes(hm: int) -> float:
    hours = hm // 100
    minutes = hm % 100
    return float(hours * 60 + minutes)


def _sigmoid(z):
    z = np.clip(z, -30, 30)
    return 1 / (1 + np.exp(-z))


async def _fetch_training_rows(db) -> List[Dict[str, Any]]:
    """
    Joins signal_features (situational context) with signals (strategy
    identity via breakout_status) for every record with a clean win/loss
    outcome. This join happens at TRAIN TIME, not at logging time — so it
    works retroactively on ALL existing data without needing any logging
    changes or losing historical records.
    """
    from bson import ObjectId

    cursor = db.signal_features.find({"out": {"$in": [1, 2]}})
    feature_docs = await cursor.to_list(length=20000)

    sig_ids = []
    for d in feature_docs:
        try:
            sig_ids.append(ObjectId(d["sig_id"]))
        except Exception:
            continue

    signals_cursor = db.signals.find({"_id": {"$in": sig_ids}}, {"breakout_status": 1})
    signal_docs = await signals_cursor.to_list(length=20000)
    strategy_lookup = {str(s["_id"]): s.get("breakout_status", "UNKNOWN") for s in signal_docs}

    rows = []
    for d in feature_docs:
        strategy = strategy_lookup.get(d["sig_id"])
        if not strategy:
            continue
        rows.append({
            "delta": float(d.get("delta", 0.5)),
            "iv": float(d.get("iv", 13.5)),
            "dir": float(d.get("dir", 1)),
            "mom": float(d.get("mom", 0)),
            "minutes_of_day": _hm_to_minutes(int(d.get("hm", 930))),
            "idx": d.get("idx", "NI"),
            "strategy": strategy,
            "label": 1 if d.get("out") == 1 else 0,
        })
    return rows


def _build_categories(rows: List[Dict[str, Any]]) -> Tuple[List[str], List[str]]:
    idx_set = sorted(set(r["idx"] for r in rows))
    strat_counts = Counter(r["strategy"] for r in rows)
    strategy_categories = sorted([s for s, c in strat_counts.items() if c >= MIN_STRATEGY_SAMPLES])
    strategy_categories.append("OTHER")
    return idx_set, strategy_categories


def _row_to_vector(row, idx_categories, strategy_categories, numeric_mean, numeric_std) -> np.ndarray:
    numeric = np.array([row[f] for f in NUMERIC_FIELDS], dtype=float)
    numeric_scaled = (numeric - numeric_mean) / np.where(numeric_std == 0, 1, numeric_std)

    idx_onehot = [1.0 if row["idx"] == c else 0.0 for c in idx_categories]

    strat = row["strategy"] if row["strategy"] in strategy_categories else "OTHER"
    strat_onehot = [1.0 if strat == c else 0.0 for c in strategy_categories]

    return np.concatenate([numeric_scaled, idx_onehot, strat_onehot])


async def train_model(db) -> Dict[str, Any]:
    """
    Trains a lightweight logistic regression (pure NumPy — no sklearn, to keep
    memory footprint tiny on the Render instance) predicting P(TARGET_HIT)
    from situational features + strategy identity. Saved to MongoDB so it
    survives restarts without retraining.
    """
    rows = await _fetch_training_rows(db)
    if len(rows) < 100:
        return {"success": False, "message": f"Only {len(rows)} usable rows — need at least 100."}

    idx_categories, strategy_categories = _build_categories(rows)

    numeric_matrix = np.array([[r[f] for f in NUMERIC_FIELDS] for r in rows], dtype=float)
    numeric_mean = numeric_matrix.mean(axis=0)
    numeric_std = numeric_matrix.std(axis=0)

    X = np.array([
        _row_to_vector(r, idx_categories, strategy_categories, numeric_mean, numeric_std)
        for r in rows
    ])
    y = np.array([r["label"] for r in rows], dtype=float)

    n = len(rows)
    rng = np.random.default_rng(42)
    perm = rng.permutation(n)
    split = int(n * 0.8)
    train_idx, val_idx = perm[:split], perm[split:]
    X_train, y_train = X[train_idx], y[train_idx]
    X_val, y_val = X[val_idx], y[val_idx]

    n_features = X_train.shape[1]
    weights = np.zeros(n_features)
    bias = 0.0

    for _ in range(ITERATIONS):
        z = X_train @ weights + bias
        preds = _sigmoid(z)
        error = preds - y_train
        grad_w = (X_train.T @ error) / len(y_train) + L2_REG * weights
        grad_b = error.mean()
        weights -= LEARNING_RATE * grad_w
        bias -= LEARNING_RATE * grad_b

    train_preds = _sigmoid(X_train @ weights + bias)
    train_acc = float(((train_preds >= 0.5).astype(int) == y_train).mean())

    val_preds = _sigmoid(X_val @ weights + bias) if len(y_val) > 0 else np.array([])
    val_acc = float(((val_preds >= 0.5).astype(int) == y_val).mean()) if len(y_val) > 0 else 0.0

    baseline_win_rate = float(y.mean())
    high_conf_mask = val_preds >= 0.6
    filtered_win_rate = float(y_val[high_conf_mask].mean()) if high_conf_mask.sum() > 0 else 0.0
    filtered_coverage = float(high_conf_mask.mean()) if len(val_preds) > 0 else 0.0

    model_doc = {
        "_id": MODEL_DOC_ID,
        "weights": weights.tolist(),
        "bias": float(bias),
        "numeric_fields": NUMERIC_FIELDS,
        "numeric_mean": numeric_mean.tolist(),
        "numeric_std": numeric_std.tolist(),
        "idx_categories": idx_categories,
        "strategy_categories": strategy_categories,
        "trained_at": datetime.utcnow(),
        "n_samples": n,
        "train_accuracy": round(train_acc, 4),
        "val_accuracy": round(val_acc, 4),
        "baseline_win_rate": round(baseline_win_rate, 4),
        "filtered_win_rate_at_60pct_threshold": round(filtered_win_rate, 4),
        "filtered_coverage_at_60pct_threshold": round(filtered_coverage, 4),
    }
    await db.ml_models.update_one({"_id": MODEL_DOC_ID}, {"$set": model_doc}, upsert=True)

    global _cached_model, _cache_loaded_at
    _cached_model = model_doc
    _cache_loaded_at = datetime.utcnow()

    logger.info(
        f"🧠 [ML TRAIN] {n} samples | Train acc {train_acc:.3f} | Val acc {val_acc:.3f} | "
        f"Baseline WR {baseline_win_rate:.3f} -> Filtered WR (conf>=60%) {filtered_win_rate:.3f} "
        f"(covers {filtered_coverage*100:.1f}% of signals)."
    )
    return {"success": True, "report": {k: v for k, v in model_doc.items() if k not in ("weights", "bias")}}


async def _get_model(db):
    global _cached_model, _cache_loaded_at
    now = datetime.utcnow()
    if _cached_model and _cache_loaded_at and (now - _cache_loaded_at).total_seconds() < CACHE_TTL_SECONDS:
        return _cached_model
    doc = await db.ml_models.find_one({"_id": MODEL_DOC_ID})
    _cached_model = doc
    _cache_loaded_at = now
    return doc


async def predict_confidence(
    db, index_name: str, strategy_key: str, delta: float, iv: float,
    selected_type: str, mom_bias: Optional[str], hm: int
) -> Optional[float]:
    """
    Returns predicted probability (0-1) of TARGET_HIT for a new signal, or
    None if no model has been trained yet. SHADOW MODE — this does not gate or
    block anything; callers just store the value for later comparison via
    /admin/ml-shadow-performance.
    """
    model = await _get_model(db)
    if not model:
        return None
    try:
        dir_code = 1 if selected_type == "CE" else -1
        mom_code = 1 if mom_bias == "CE" else (-1 if mom_bias == "PE" else 0)

        row = {
            "delta": delta, "iv": iv, "dir": dir_code, "mom": mom_code,
            "minutes_of_day": _hm_to_minutes(hm),
            "idx": index_name[:2].upper(),
            "strategy": f"STRAT_{strategy_key}",
        }
        numeric_mean = np.array(model["numeric_mean"])
        numeric_std = np.array(model["numeric_std"])
        vec = _row_to_vector(row, model["idx_categories"], model["strategy_categories"], numeric_mean, numeric_std)
        weights = np.array(model["weights"])
        z = float(vec @ weights + model["bias"])
        return round(float(_sigmoid(np.array([z]))[0]), 4)
    except Exception as e:
        logger.warning(f"⚠️ ML prediction failed (non-critical, shadow mode continues): {str(e)}")
        return None