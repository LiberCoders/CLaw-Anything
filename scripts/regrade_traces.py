#!/usr/bin/env python3
"""Re-grade existing traces with GenericStateGrader (no per-task gold, no judge
by default) and report how the deterministic state+grounding signal reclassifies
trajectories vs the legacy stored scores.

Usage:
    python scripts/regrade_traces.py [--judge] [--gt /tmp/joined.json]

Walks traces/**/batch_results.json to enumerate trials (re-using stored dimension
scores for the "old" formula), loads each trace, runs GenericStateGrader, and
prints a confusion matrix against an optional ground-truth file.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))

import yaml  # noqa: E402

from claw_anything.graders.regrade import GenericStateGrader  # noqa: E402
from claw_anything.trace.reader import load_trace  # noqa: E402
from claw_anything.models.task import TaskDefinition  # noqa: E402
from claw_anything.models.scoring import compute_task_score, is_pass  # noqa: E402
from claw_anything.models.trace import DimensionScores  # noqa: E402


def _load_task(trace_path: str, task_id: str) -> TaskDefinition | None:
    """Locate the gen_tasks task.yaml for a trace (strip _oh from batch, hash from dir)."""
    import re
    trial_dir = os.path.dirname(trace_path)
    task_dir_name = re.sub(r"_[0-9a-f]{8}$", "", os.path.basename(trial_dir))
    batch = os.path.basename(os.path.dirname(os.path.dirname(trial_dir)))
    gen_batch = re.sub(r"_oh$", "", batch)
    cand = os.path.join(REPO, "gen_tasks", gen_batch, task_dir_name, "task.yaml")
    if not os.path.exists(cand):
        return None
    try:
        data = yaml.safe_load(open(cand))
        return TaskDefinition(**data)
    except Exception:
        # minimal stub: only prompt + judge_rubric are needed by GenericStateGrader
        return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--judge", action="store_true", help="also call the LLM judge")
    ap.add_argument("--gt", default="/tmp/joined.json", help="ground-truth file with gt2 labels")
    ap.add_argument("--traces", default=os.path.join(REPO, "traces"))
    ap.add_argument("--out", default="/tmp/regrade_results.json")
    args = ap.parse_args()

    gt = {}
    if os.path.exists(args.gt):
        for r in json.load(open(args.gt)):
            if "gt2" in r:
                gt[(r["task"], int(r["trial"]))] = r["gt2"]

    judge = None
    if args.judge:
        from claw_anything.config import load_config
        from claw_anything.graders.llm_judge import LLMJudge
        cfg = load_config(os.path.join(REPO, "config.yaml"))
        judge = LLMJudge(
            model_id=cfg.judge.model_id, api_key=cfg.judge.api_key,
            base_url=cfg.judge.base_url, tls_verify=cfg.judge.tls_verify,
        )
        print(f"[judge] {cfg.judge.model_id} @ {cfg.judge.base_url}")

    grader = GenericStateGrader()
    rows = []
    for br in sorted(glob.glob(os.path.join(args.traces, "*/*/batch_results.json"))):
        d = json.load(open(br))
        for task in d:
            tid = task["task_id"]
            for i, t in enumerate(task["trials"]):
                trace = t.get("trace")
                if not trace or not os.path.exists(trace):
                    continue
                tdef = _load_task(trace, tid)
                if tdef is None:
                    continue
                try:
                    start, messages, dispatches, end, audit = load_trace(trace)
                except Exception as e:
                    print(f"[skip] {tid} t{i}: {e}")
                    continue
                scores = grader.grade(messages, dispatches, tdef, audit_data=audit,
                                      judge=judge)
                new_score = compute_task_score(scores)
                rows.append({
                    "task": tid, "trial": i,
                    "legacy_completion": t.get("completion"),
                    "new_completion": scores.completion,
                    "judge_quality": getattr(grader, "last_quality", None),
                    "grounding": getattr(grader, "last_grounding", None),
                    "new_task_score": new_score,
                    "new_pass": is_pass(new_score),
                    "failures": getattr(grader, "last_failures", []),
                    "unsupported": getattr(grader, "last_unsupported", []),
                    "gt": gt.get((tid, i)),
                })
    json.dump(rows, open(args.out, "w"), indent=1)
    print(f"regraded {len(rows)} trials -> {args.out}")

    # confusion vs ground truth
    labeled = [r for r in rows if r["gt"] in ("correct", "incorrect")]
    if labeled:
        tp = sum(1 for r in labeled if r["new_pass"] and r["gt"] == "correct")
        fp = sum(1 for r in labeled if r["new_pass"] and r["gt"] == "incorrect")
        tn = sum(1 for r in labeled if not r["new_pass"] and r["gt"] == "incorrect")
        fn = sum(1 for r in labeled if not r["new_pass"] and r["gt"] == "correct")
        n = len(labeled)
        npass = sum(1 for r in labeled if r["new_pass"])
        ncorrect = sum(1 for r in labeled if r["gt"] == "correct")
        print(f"\nground-truth labeled trials: {n}  (correct={ncorrect}, {100*ncorrect/n:.0f}%)")
        print(f"GenericStateGrader pass: {npass}/{n} = {100*npass/n:.0f}%")
        print(f"ACC={100*(tp+tn)/n:.1f}%  TP={tp} FP={fp} TN={tn} FN={fn} "
              f"prec={tp/(tp+fp) if tp+fp else 0:.2f} rec={tp/(tp+fn) if tp+fn else 0:.2f}")
        caught = sum(1 for r in labeled if not r["new_pass"] and r["gt"] == "incorrect"
                     and (r["failures"] or r["unsupported"]))
        print(f"incorrect trials caught WITH a concrete reason (silent-fail/fabrication): {caught}")


if __name__ == "__main__":
    main()
