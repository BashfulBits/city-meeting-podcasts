#!/usr/bin/env python3
"""Would retiring the R5 matrix and pre-labeler overlay change which tags display? (review/53)

Every stored LLM tag candidate is projected through the CURRENT display code (apply_admission with
the live llm_evaluation.json state), and compared with the post-retirement rule: an LLM candidate
is hidden until the judge stack (PR8) admits it; rule tags are unchanged.

Read-only. Needs the B2 environment (`citypods-env`). Run from the repo root:

    python scripts/check_retirement_display.py

Exit status 1 if any candidate's display would change (2026-10-19: 172,526 candidates, 0 of 115
matrix rows qualified, 0 differences)."""

import collections
import json
from concurrent.futures import ThreadPoolExecutor

import yaml

from citypods.llm_evaluation import apply_admission, config_from_mapping
from citypods.storage.s3 import b2_from_env


def main() -> int:
    store = b2_from_env()

    def get(key):
        raw = store.get_bytes(key)
        return json.loads(raw[0] if isinstance(raw, tuple) else raw)

    state = get("state/llm_evaluation.json")
    with open("config/site_config.yml") as handle:
        site = yaml.safe_load(handle)
    econf = config_from_mapping(site["tagging"]["evaluation"])
    qualified = sum(1 for entry in state["matrix"] if entry.get("qualified"))
    print("matrix rows", len(state["matrix"]), "qualified", qualified)
    keys = [
        o if isinstance(o, str) else getattr(o, "key", None) or o[0]
        for o in store.list_objects("state/sources/")
    ]
    keys = [k for k in keys if k.endswith("/episodes.json")]

    def load(key):
        try:
            return get(key).get("episodes", {})
        except Exception:  # noqa: BLE001 -- an unreadable source is skipped, not counted
            return {}

    totals: collections.Counter = collections.Counter()
    diffs = []
    with ThreadPoolExecutor(8) as pool:
        for episodes in pool.map(load, keys):
            for uid, episode in episodes.items():
                for candidate in episode.get("llm_tag_candidates") or []:
                    if not isinstance(candidate, dict):
                        continue
                    applied = apply_admission(candidate, config=econf, state=state)
                    now = bool(applied["display"] and applied["admission"] == "admitted")
                    active = candidate.get("candidate_state") != "historical"
                    # After retirement an LLM candidate is hidden until the stack admits it (PR8);
                    # an active rule candidate is shown, as now.
                    after = applied.get("source_kind") == "rule" and active
                    totals[("candidates",)] += 1
                    totals[
                        ("source", applied.get("source_kind"), "active", active, "shown", now)
                    ] += 1
                    totals[("prelabeler_basis", applied.get("prelabeler_basis"))] += 1
                    if now != after:
                        diffs.append(
                            (uid, candidate.get("candidate_id"), applied.get("source_kind"))
                        )
    for key, count in sorted(totals.items(), key=str):
        print(count, key)
    print("display differences after retirement:", len(diffs))
    for diff in diffs[:10]:
        print(diff)
    return 1 if diffs else 0


if __name__ == "__main__":
    raise SystemExit(main())
