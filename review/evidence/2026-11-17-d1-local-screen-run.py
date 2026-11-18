"""Local screen of a Liquid d1 model over the frozen judge-lane inputs (no network at run time)."""

import json
import sys
import time

import torch
from transformers import AutoModel

name, rev, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
MAX_TOKENS_PER_CALL = int(sys.argv[4]) if len(sys.argv) > 4 else 12000
data = json.load(open("/private/tmp/claude-501/screen/inputs.json"))

device = "mps" if torch.backends.mps.is_available() else "cpu"
dtype = torch.bfloat16 if device == "mps" else torch.float32
t0 = time.time()
model = AutoModel.from_pretrained(
    name, revision=(None if name.startswith("/") else rev), trust_remote_code=True, dtype=dtype
).to(device)
load_s = round(time.time() - t0, 1)


def est(q):
    return int(len(json.dumps(q).split()) * 1.35) + 20


def chunks(questions):
    cur, n = {}, 0
    for k, q in questions.items():
        c = est(q)
        if cur and n + c > MAX_TOKENS_PER_CALL:
            yield cur
            cur, n = {}, 0
        cur[k] = q
        n += c
    if cur:
        yield cur


result = {
    "model": name,
    "revision": rev,
    "device": device,
    "dtype": str(dtype),
    "load_s": load_s,
    "runs": {},
}
for run, body in data["runs"].items():
    answers, invalid, calls, t1 = {}, 0, 0, time.time()
    for part in chunks(body["questions"]):
        calls += 1
        try:
            out = model.system_one(body["state"], part)
        except Exception as exc:  # recorded, never hidden
            invalid += len(part)
            result.setdefault("errors", []).append(f"{run}: {type(exc).__name__}: {str(exc)[:200]}")
            continue
        for k in part:
            a = out["answers"].get(k, {})
            v = a.get("noul")
            if v is None or v != v:
                invalid += 1
            else:
                answers[k] = float(v)
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
json.dump(result, open(out_path, "w"))
