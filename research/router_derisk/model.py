"""Reconstruct the note's seeded experiment, including uncertainty and auditable errors."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path

import numpy as np
import pandas as pd
from schema import FEATURES
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import confusion_matrix, roc_auc_score, roc_curve
from sklearn.model_selection import StratifiedKFold, cross_val_score, train_test_split
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.tree import DecisionTreeClassifier

SEED = 13


def logistic(**kwargs):
    return make_pipeline(
        StandardScaler(),
        LogisticRegression(max_iter=5000, random_state=SEED, **kwargs),
    )


def measure(frame: pd.DataFrame, bootstrap_samples: int = 4000) -> dict:
    """Fit only on train, then compute held-out diagnostics without tuning on test."""
    if not frame["class"].isin(["OCR", "NOCR"]).all():
        raise ValueError("class must be OCR or NOCR")
    x = frame[FEATURES].to_numpy(dtype=float)
    y = (frame["class"] == "OCR").to_numpy(dtype=int)
    if not np.isfinite(x).all():
        raise ValueError("features contain non-finite values")
    train, test = train_test_split(
        np.arange(len(y)), test_size=0.30, random_state=SEED, stratify=y,
    )
    primary = logistic().fit(x[train], y[train])
    train_scores = primary.predict_proba(x[train])[:, 1]
    scores = primary.predict_proba(x[test])[:, 1]
    fpr, tpr, thresholds = roc_curve(y[train], train_scores)
    threshold = float(thresholds[np.argmax(tpr - fpr)])
    predictions = (scores >= threshold).astype(int)
    tn, fp, fn, tp = confusion_matrix(y[test], predictions, labels=[0, 1]).ravel()

    rng = np.random.default_rng(SEED)
    boot = []
    for _ in range(bootstrap_samples):
        indices = rng.integers(0, len(test), size=len(test))
        if np.unique(y[test][indices]).size == 2:
            boot.append(float(roc_auc_score(y[test][indices], scores[indices])))
    if len(boot) < 2:
        raise ValueError("fewer than two valid bootstrap replicates")
    cv = cross_val_score(
        logistic(), x, y, scoring="roc_auc",
        cv=StratifiedKFold(n_splits=5, shuffle=True, random_state=SEED),
    )

    variants = {}
    log_x = x.copy()
    log_x[:, [0, 1, 2, 4, 5]] = np.log1p(log_x[:, [0, 1, 2, 4, 5]])
    candidates = [
        ("raw_fields", logistic(), x[:, :5]),
        ("class_weighted", logistic(class_weight="balanced"), x),
        ("log_counts", logistic(), log_x),
    ]
    candidates += [
        (f"tree_depth_{depth}", DecisionTreeClassifier(max_depth=depth, random_state=SEED), x)
        for depth in range(1, 5)
    ]
    for name, estimator, data in candidates:
        estimator.fit(data[train], y[train])
        variants[name] = {
            "train_auc": float(roc_auc_score(y[train], estimator.predict_proba(data[train])[:, 1])),
            "test_auc": float(roc_auc_score(y[test], estimator.predict_proba(data[test])[:, 1])),
        }
    # Directions are specified by the note, never selected by inspecting test labels.
    directions = dict.fromkeys(FEATURES, 1)
    directions.update(dict.fromkeys(
        ["characters_per_page", "character_count", "text_coverage", "pages_with_text"], -1,
    ))
    signals = {
        name: {
            "direction": directions[name],
            "raw_auc": float(roc_auc_score(y[test], x[test, column])),
            "ocr_direction_auc": float(roc_auc_score(y[test], directions[name] * x[test, column])),
        }
        for column, name in enumerate(FEATURES)
    }
    held_out = frame.iloc[test].copy()
    held_out["score"] = scores
    held_out["prediction"] = predictions
    false_negatives = held_out[(y[test] == 1) & (predictions == 0)]
    false_positives = held_out[(y[test] == 0) & (predictions == 1)]
    ocr = held_out[y[test] == 1]
    return {
        "seed": SEED,
        "feature_order": FEATURES,
        "train_rows": len(train),
        "train_ocr": int(y[train].sum()),
        "test_rows": len(test),
        "test_ocr": int(y[test].sum()),
        "train_auc": float(roc_auc_score(y[train], train_scores)),
        "test_auc": float(roc_auc_score(y[test], scores)),
        "bootstrap": {
            "requested": bootstrap_samples, "valid": len(boot),
            "ci95": np.quantile(boot, [0.025, 0.975]).tolist(),
            "se": float(np.std(boot, ddof=1)),
        },
        "cv": {"folds": cv.tolist(), "mean": float(cv.mean()), "std": float(cv.std())},
        "variants": variants,
        "signals": signals,
        "training_youden_threshold": threshold,
        "confusion": dict(zip(("tn", "fp", "fn", "tp"), map(int, (tn, fp, fn, tp)), strict=True)),
        "error_shape": {
            "fn_full_text_no_image": int((
                (false_negatives.text_coverage == 1) & (false_negatives.image_dominated_share == 0)
            ).sum()),
            "ocr_without_text": int((ocr.text_coverage == 0).sum()),
            "ocr_with_text": int((ocr.text_coverage > 0).sum()),
            "fn_annotations": false_negatives.annotation.value_counts().to_dict(),
            "ocr_annotations": ocr.annotation.value_counts().to_dict(),
            "fp_at_most_two_pages": int((false_positives.page_count <= 2).sum()),
            "fp_image_share_above_half": int((false_positives.image_dominated_share > 0.5).sum()),
        },
        "false_negatives": false_negatives.to_dict(orient="records"),
        "false_positives": false_positives.to_dict(orient="records"),
        "test_filenames": held_out.filename.tolist(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--features", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    frame = pd.read_csv(args.features)
    if len(frame) != 1620 or int((frame["class"] == "OCR").sum()) != 227:
        raise ValueError("expected 1,620 documents including 227 OCR documents")
    if frame.filename.duplicated().any():
        raise ValueError("duplicate filenames")
    result = measure(frame)
    result["features_sha256"] = hashlib.sha256(args.features.read_bytes()).hexdigest()
    result["versions"] = {
        name: importlib.metadata.version(name) for name in ("numpy", "pandas", "scikit-learn")
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in ("test_auc", "bootstrap", "confusion")}, indent=2))


if __name__ == "__main__":
    main()
