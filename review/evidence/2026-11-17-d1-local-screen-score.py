import json
import statistics
import sys


def auc(pos, neg):
    if not pos or not neg:
        return None
    return sum((p > n) + 0.5 * (p == n) for p in pos for n in neg) / (len(pos) * len(neg))


def rank(xs):
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    for pos, i in enumerate(order):
        r[i] = pos
    return r


def spearman(a, b):
    ra, rb = rank(a), rank(b)
    ma, mb = statistics.mean(ra), statistics.mean(rb)
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb, strict=True))
    den = (sum((x - ma) ** 2 for x in ra) * sum((y - mb) ** 2 for y in rb)) ** 0.5
    return num / den if den else None


inp = json.load(open("/private/tmp/claude-501/screen/inputs.json"))
truth = inp["truth"]["bundling"]
controls = set(inp["truth"]["controls"])
jev = inp["jev"]


def bundling(ans):
    ids = [k for k in ans if k in truth]
    acc = sum((ans[k] >= 0.5) == truth[k] for k in ids) / len(ids) if ids else None
    a = auc([ans[k] for k in ids if truth[k]], [ans[k] for k in ids if not truth[k]])
    return {
        "n": len(ids),
        "acc": None if acc is None else round(acc, 3),
        "auc": None if a is None else round(a, 3),
    }


def context(ans, ref):
    real = [k for k in ans if k not in controls]
    ctl = [k for k in ans if k in controls]
    a = auc([ans[k] for k in real], [ans[k] for k in ctl])
    shared = [k for k in real if k in ref]
    agree = (
        sum((ans[k] >= 0.5) == (ref[k] >= 0.5) for k in shared) / len(shared) if shared else None
    )
    sp = spearman([ans[k] for k in shared], [ref[k] for k in shared]) if len(shared) > 2 else None
    return {
        "controls_accepted": sum(ans[k] >= 0.5 for k in ctl),
        "auc_real_vs_controls": None if a is None else round(a, 3),
        "real_accept_rate": round(sum(ans[k] >= 0.5 for k in real) / len(real), 3)
        if real
        else None,
        "agree_with_jev": None if agree is None else round(agree, 3),
        "spearman_vs_jev": None if sp is None else round(sp, 3),
    }


print("== JEV reference (2026-09-30)")
for k, v in jev["bundling"].items():
    print(" bundling", k, bundling(v))
for t in ("T0", "T1", "T2"):
    print(" context", t, context(jev["context-p2"][t], jev["context-p2"][t]))
for path in sys.argv[1:]:
    r = json.load(open(path))
    print(
        f"== {r['model']} rev {r['revision'][:8]} on {r['device']} {r['dtype']} load {r['load_s']}s"
    )
    for run, v in r["runs"].items():
        if run.startswith("bundling"):
            print(
                " ",
                run,
                bundling(v["answers"]),
                "invalid",
                v["invalid"],
                "calls",
                v["calls"],
                v["seconds"],
                "s",
            )
        else:
            tier = run.split("-")[1]
            print(
                " ",
                run,
                context(v["answers"], jev["context-p2"][tier]),
                "invalid",
                v["invalid"],
                "calls",
                v["calls"],
                v["seconds"],
                "s",
            )
    if r.get("errors"):
        print("  errors:", r["errors"][:3])
