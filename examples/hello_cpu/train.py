"""Minimal single-file submission. Uses only the Python standard library."""
import json
import os
from pathlib import Path

x = list(range(100))
y = [2 * t + 3 for t in x]
x_mean, y_mean = sum(x) / len(x), sum(y) / len(y)
slope = sum((a-x_mean)*(b-y_mean) for a, b in zip(x, y)) / sum((a-x_mean)**2 for a in x)
coefficients = {"slope": slope, "intercept": y_mean-slope*x_mean, "samples": len(x)}
target = Path(os.environ["GPUHUB_OUTPUT_DIR"]) / "fit.json"
target.write_text(json.dumps(coefficients), encoding="utf-8")
print(f"Fit {len(x)} examples. Saved {target.name}.", flush=True)
