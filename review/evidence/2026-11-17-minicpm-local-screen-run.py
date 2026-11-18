"""Local screen of MiniCPM5-2B-Jev over the frozen judge-lane inputs (in-process, no server)."""

import json
import os
import sys
import time

import torch

snap, out_path = sys.argv[1], os.path.abspath(sys.argv[2])
MAX_TOKENS_PER_CALL = int(sys.argv[3]) if len(sys.argv) > 3 else 3000
sys.path.insert(0, snap)
os.chdir(snap)
from model import MiniCPMSystemOne, to_internal_record  # noqa: E402

data = json.load(open("/private/tmp/claude-501/screen/inputs.json"))
t0 = time.time()
model, tok = MiniCPMSystemOne.load_checkpoint(snap, device="mps", dtype=torch.bfloat16)
load_s = round(time.time() - t0, 1)


def est(q):
    return int(len(json.dumps(q).split()) * 1.35) + 20


def chunks(questions, limit):
    cur, n = {}, 0
    for k, q in questions.items():
        c = est(q)
        if cur and n + c > limit:
            yield cur
            cur, n = {}, 0
        cur[k] = q
        n += c
    if cur:
        yield cur


def score(state, part, answers, errors, run):
    try:
        internal = to_internal_record({"state": state, "questions": part})
        results = model.predict_record(internal, use_pride=False)
    except Exception as exc:
        if len(part) > 1:  # context overflow or memory: halve and retry
            keys = list(part)
            half = len(keys) // 2
            return score(state, {k: part[k] for k in keys[:half]}, answers, errors, run) + score(
                state, {k: part[k] for k in keys[half:]}, answers, errors, run
            )
        errors.append(f"{run}: {type(exc).__name__}: {str(exc)[:300]}")
        return 1
    for q, (_, probs) in zip(internal["questions"], results, strict=True):
        answers[q["name"]] = float(probs.tolist()[1])
    return 0


result = {
    "model": "ytbai/MiniCPM5-2B-Jev",
    "revision": os.path.basename(snap),
    "device": "mps",
    "dtype": "bfloat16",
    "load_s": load_s,
    "runs": {},
}
errors = []
for run, body in data["runs"].items():
    answers, calls, t1, invalid = {}, 0, time.time(), 0
    for part in chunks(body["questions"], MAX_TOKENS_PER_CALL):
        calls += 1
        invalid += score(body["state"], part, answers, errors, run)
    invalid = len(body["questions"]) - len(answers)
    result["runs"][run] = {
        "answers": answers,
        "invalid": invalid,
        "calls": calls,
        "seconds": round(time.time() - t1, 1),
    }
    print(
        run,
        len(answers),
        "invalid",
        invalid,
        "calls",
        calls,
        result["runs"][run]["seconds"],
        "s",
        flush=True,
    )
if errors:
    result["errors"] = errors[:10]
json.dump(result, open(out_path, "w"))
