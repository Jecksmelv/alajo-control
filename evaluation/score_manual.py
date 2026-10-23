"""Score a completed manual answer sheet against ground truth and record the human result.

    python -m evaluation.score_manual evaluation/manual_test_packet/answer_sheet.csv
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from backend import config  # noqa: E402
from evaluation.common import all_scenarios, load_gt  # noqa: E402


def main(path: str) -> None:
    key = json.loads((PROJECT_DIR / "evaluation" / "manual_test_packet" / "item_key.json").read_text(encoding="utf-8"))
    gt = {s["scenario_id"]: s for s in all_scenarios(load_gt())}
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    correct, scored, minutes = 0, 0, None
    for r, k in zip(rows, key):
        if r["item"] == "01":
            try:
                minutes = float((r.get("notes") or "").split()[0])
            except (ValueError, IndexError):
                minutes = None
        exp = gt.get(k["scenario_id"], {}).get("expected", {}).get("disposition")
        if k["scenario_id"].startswith("ROUTINE_EXACT"):
            exp = "AUTO_POST_EXACT"
        elif k["scenario_id"].startswith("ROUTINE_OVERDUE"):
            exp = "ROUTINE_REMINDER"
        if r["disposition"].strip():
            scored += 1
            correct += int(r["disposition"].strip().upper() == exp)
    out_path = config.OUTPUT_DIR / "manual_benchmark.json"
    res = json.loads(out_path.read_text(encoding="utf-8"))
    res["human_result"] = {"items_answered": scored, "correct": correct, "minutes": minutes}
    out_path.write_text(json.dumps(res, indent=1), encoding="utf-8")
    print(res["human_result"])


if __name__ == "__main__":
    main(sys.argv[1])
