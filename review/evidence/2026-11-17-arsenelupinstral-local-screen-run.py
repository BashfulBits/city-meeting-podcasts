"""Local screen of ArseneLupinstral over the frozen judge-lane inputs (in-process, no server)."""

import json
import os
import sys
import time

import yaml

snap, out_path = sys.argv[1], os.path.abspath(sys.argv[2])
MAX_TOKENS_PER_CALL = int(sys.argv[3]) if len(sys.argv) > 3 else 6000
sys.path.insert(0, "/private/tmp/claude-501/screen/arsene-src")
os.chdir(snap)
from arsenelupin.schema import Request, configure_limits  # noqa: E402
from arsenelupin.serve import charge_moteur  # noqa: E402

config = yaml.safe_load(open("serve.yaml"))
config["model"]["device"] = "mps"
config.setdefault("engine", {})["mode"] = os.environ.get("ARSENE_ENGINE", "independent")
config["engine"]["branch_microbatch"] = 1
config["engine"]["max_batch_tokens"] = 8192
configure_limits(config)
data = json.load(open("/private/tmp/claude-501/screen/inputs.json"))
t0 = time.time()
moteur = charge_moteur(
    config, None, None, "calibration/default.json", config["engine"].get("formulation", "blind")
)
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
    "model": "IBOU-SEARCH/ArseneLupinstral-v1.0",
    "revision": os.path.basename(snap),
    "device": "mps",
    "dtype": "bfloat16",
    "load_s": load_s,
    "engine": config["engine"]["mode"],
    "runs": {},
}
for run, body in data["runs"].items():
    answers, invalid, calls, t1 = {}, 0, 0, time.time()
    for part in chunks(body["questions"]):
        calls += 1
        try:
            resp = moteur.score_serie(
                Request.model_validate({"state": body["state"], "questions": part})
            )
        except Exception as exc:  # recorded, never hidden
            invalid += len(part)
            result.setdefault("errors", []).append(f"{run}: {type(exc).__name__}: {str(exc)[:300]}")
            continue
        for k in part:
            ans = resp.answers.get(k)
            if ans is None:
                invalid += 1
            else:
                answers[k] = float(ans.p_yes)
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
json.dump(result, open(out_path, "w"))
