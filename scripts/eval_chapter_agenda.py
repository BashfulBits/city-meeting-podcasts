"""Score models on the committed chapter-agenda evaluation set (GH#1852; first task covered).

Ground truth is the meeting provider's own published chapters (``evals/chapter-agenda/gold.json``),
never model output. Each model gets production's exact request (``build_agenda_job``) and
production's post-processing (``finalize_agenda_job``: strict validation plus the recovery layer),
then the generated agenda items are matched to the provider chapters with the project's
span-overlap matcher (``audit_locator_crosswalk._pair_features`` / ``_chapter_status``).

    citypods-env python scripts/eval_chapter_agenda.py --model tencent/hy3 --model <another>
    citypods-env python scripts/eval_chapter_agenda.py --freeze --state-dir <pulled state>
        (re-freezes the episode set; a reviewed change that bumps its version)

Metrics per model (all over the same frozen episodes):
  valid_rate  answered episodes whose response passed production finalization / answered episodes
              (a provider error is retried, then counted as unanswered -- never as invalid output;
              a model with more than 10% unanswered is reported inconclusive)
  recall      provider chapters with a strong or possible match / provider chapters
  precision   items matched one-to-one to a provider chapter / items that match any chapter
              (items with no chapter are grounded agenda items the provider did not chapter --
              reported as extra_items, not counted as errors)
  f1          harmonic mean of precision and recall
Recall and precision are computed over valid episodes only, so an invalid response lowers
valid_rate rather than silently counting as "no items".

Admission rule (maintainer decision 2026-09-24, see evals/chapter-agenda/README.md): a candidate
beats a baseline when its F1 is higher, its precision is not lower, and its valid_rate is >= 95%.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "research" / "agenda_chapters"))

from audit_locator_crosswalk import (  # noqa: E402
    _chapter_status,
    _item_status,
    _pair_features,
)

from citypods.chapter_jobs import (  # noqa: E402
    _response_content,
    build_agenda_job,
    finalize_agenda_job,
)
from citypods.chapter_titles import recover_agenda_item_extractor_response  # noqa: E402
from citypods.compute.base import JobResult  # noqa: E402
from citypods.compute.llm import LLMStructuredOutputError  # noqa: E402

EVAL_DIR = REPO_ROOT / "evals" / "chapter-agenda"
# `main` is the set results and repairs were studied on; `holdout` is never inspected while a
# change is designed, so a validator or prompt change that only fits `main` shows up there.
SPLIT_DIRS = {"main": EVAL_DIR, "holdout": EVAL_DIR / "holdout"}
MIN_VALID_RATE = 0.95
# More unanswered episodes than this makes a model's run inconclusive rather than a result.
MAX_UNANSWERED_RATE = 0.10
RETRY_DELAYS_SECONDS = (20, 60, 180)
WORKER_RESPONSE_SECONDS = 720.0  # workers/llm-dispatch-v2 MAX_RESPONSE_SECONDS


def _load(name: str, split: str = "main") -> dict[str, Any]:
    return json.loads((SPLIT_DIRS[split] / name).read_text(encoding="utf-8"))


def score_episode(items: list[Mapping[str, Any]], chapters: list[Mapping[str, Any]]) -> dict:
    """Match generated items to provider chapters exactly as the original benchmark did."""
    pairs = [
        (ci, ii, _pair_features(str(ch.get("title") or ""), item))
        for ci, ch in enumerate(chapters)
        for ii, item in enumerate(items)
    ]
    matched_chapters = 0
    owner: dict[int, int] = {}
    ambiguous: set[int] = set()
    best_for_item: dict[int, dict] = {}
    for ci in range(len(chapters)):
        ranked = sorted(
            ((f, ii) for c, ii, f in pairs if c == ci),
            key=lambda p: (p[0]["score"], p[0]["identifier_overlap"] != [], p[1]),
            reverse=True,
        )
        top = ranked[0] if ranked else (None, None)
        second = ranked[1] if len(ranked) > 1 else (None, None)
        status = _chapter_status(top[0], second[0])
        if status in {"strong", "possible"}:
            matched_chapters += 1
        if status == "ambiguous":
            ambiguous.add(ci)
        if top[0] is not None and top[0]["score"] >= 0.60:
            owner.setdefault(top[1], ci)
    for ci, ii, f in pairs:
        if ii not in best_for_item or f["score"] > best_for_item[ii][0]["score"]:
            best_for_item[ii] = (f, ci)
    mapped = conflicted = 0
    for ii in range(len(items)):
        best, best_ci = best_for_item.get(ii, (None, None))
        best_owner_item = next((i for i, c in owner.items() if c == best_ci), None)
        status = _item_status(
            best,
            best_chapter_owner=best_owner_item,
            item_index=ii,
            chapter_ambiguous=best_ci in ambiguous,
        )
        mapped += status == "mapped"
        conflicted += status == "conflicted"
    return {
        "chapters": len(chapters),
        "matched": matched_chapters,
        "items": len(items),
        "mapped": mapped,
        "conflicted": conflicted,
    }


def _validation_detail(result: Any, agenda_text: str) -> dict:
    """Why production rejected a response: every item's outcome plus the raw output.

    Finalization stops at the first unrecovered item, so its error alone cannot show whether one
    item or many failed, or what the model actually wrote -- the evidence needed to judge whether
    the recovery layer should have rescued it.
    """
    content = _response_content(result.output)
    try:
        assessment = recover_agenda_item_extractor_response(content, agenda_text=agenda_text)
    except Exception as exc:  # noqa: BLE001 -- unparseable output has no per-item outcome
        return {"parse_error": f"{type(exc).__name__}: {exc}"[:300], "raw_response": content}
    return {
        "strict_items": len(assessment.items),
        "recovered_items": len(assessment.recovered),
        "unrecovered": [
            {"index": r.index, "display_ref": r.display_ref, "reason": r.reason}
            for r in assessment.unrecovered
        ],
        "raw_response": content,
    }


def _run_one(backend: Any, model: str, episode: Mapping[str, Any]) -> dict:
    agenda_text = episode["agenda_text"]
    source_hash = hashlib.sha256(agenda_text.encode("utf-8")).hexdigest()
    job = build_agenda_job(
        episode_uid=episode["uid"], agenda_text=agenda_text, agenda_source_hash=source_hash
    )
    # Evaluation pins exactly one model and calls it directly: without the production policy there
    # is no pool, no backup model and no Worker queue -- only the backend's configured model.
    inputs = {k: v for k, v in job.inputs.items() if k != "llm_policy"}
    started = time.monotonic()
    # A busy or rate-limited provider says nothing about the model's output quality, so transport
    # and capacity errors are retried and, if they persist, recorded as "unanswered" -- never as
    # an invalid response. Only a response that fails production finalization is invalid.
    result, last_error = None, ""
    attempt_errors: list[dict] = []
    for delay in (*RETRY_DELAYS_SECONDS, None):
        try:
            result = backend.run_inference(replace(job, inputs=inputs))
            break
        except LLMStructuredOutputError as exc:
            # The provider answered, but with nothing parseable (an empty reply included): that
            # is the model's output, so it is invalid -- not a busy provider to retry for hours.
            return {
                "uid": episode["uid"],
                "answered": True,
                "valid": False,
                "error": f"{type(exc).__name__}: {exc}"[:300],
                "validation": {"unparseable": exc.diagnostic},
                "attempt_errors": attempt_errors,
                "seconds": round(time.monotonic() - started, 1),
            }
        except Exception as exc:  # noqa: BLE001 -- provider errors are retried, then recorded
            last_error = f"{type(exc).__name__}: {exc}"[:300]
            attempt_errors.append(
                {"after_seconds": round(time.monotonic() - started, 1), "error": last_error[:160]}
            )
            if delay is None:
                break
            time.sleep(delay)
    if result is None:
        return {
            "uid": episode["uid"],
            "answered": False,
            "valid": False,
            "error": last_error,
            "attempt_errors": attempt_errors,
            "seconds": round(time.monotonic() - started, 1),
        }
    run = finalize_reply(episode, _response_content(result.output), model)
    return {
        **run,
        "attempt_errors": attempt_errors,
        "seconds": round(time.monotonic() - started, 1),
    }


def finalize_reply(episode: Mapping[str, Any], content: str, model: str) -> dict:
    """Run production finalization on one model reply; keep the reply for offline re-scoring.

    Every answered episode stores its raw reply, so a validator change can be re-scored against
    exactly the same model output (``--rescore``) instead of a fresh, noisier model run.
    """
    agenda_text = episode["agenda_text"]
    source_hash = hashlib.sha256(agenda_text.encode("utf-8")).hexdigest()
    result = JobResult(
        task="agenda-item-extract", recipe_hash="eval-chapter-agenda", output=content, model=model
    )
    try:
        artifact = finalize_agenda_job(
            result,
            episode_uid=episode["uid"],
            agenda_text=agenda_text,
            agenda_source_hash=source_hash,
            model=model,
        )
    except Exception as exc:  # noqa: BLE001 -- the output failed production validation
        return {
            "uid": episode["uid"],
            "answered": True,
            "valid": False,
            "error": f"{type(exc).__name__}: {exc}"[:300],
            "validation": _validation_detail(result, agenda_text),
            "raw_response": content,
        }
    items = [
        {"title": c.title, "evidence_text": c.evidence_text, "display_ref": c.display_ref}
        for c in artifact.items
    ]
    return {
        "uid": episode["uid"],
        "answered": True,
        "valid": True,
        "items": items,
        "raw_response": content,
    }


def summarize(runs: list[dict], gold: Mapping[str, Any]) -> dict:
    chapters_by_uid = {e["uid"]: e["chapters"] for e in gold["episodes"]}
    answered = [r for r in runs if r.get("answered")]
    valid = [r for r in runs if r["valid"]]
    totals = {"chapters": 0, "matched": 0, "items": 0, "mapped": 0, "conflicted": 0}
    for run in valid:
        s = score_episode(run["items"], chapters_by_uid[run["uid"]])
        run["score"] = s
        for key in totals:
            totals[key] += s[key]
    recall = totals["matched"] / totals["chapters"] if totals["chapters"] else 0.0
    # Every finalized item is grounded in the agenda text (finalize_agenda_job rejects the rest),
    # so an item with no provider chapter is one the provider chose not to chapter, not an error.
    # Precision therefore asks whether the items that DO correspond to a chapter are right.
    judged = totals["mapped"] + totals["conflicted"]
    precision = totals["mapped"] / judged if judged else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    seconds = sorted(r["seconds"] for r in runs)
    return {
        "episodes": len(runs),
        "answered": len(answered),
        "valid": len(valid),
        # Share of *answered* episodes whose output passed production finalization.
        "valid_rate": round(len(valid) / len(answered), 4) if answered else 0.0,
        "inconclusive": (len(runs) - len(answered)) > MAX_UNANSWERED_RATE * len(runs),
        "recall": round(recall, 4),
        "precision": round(precision, 4),
        "f1": round(f1, 4),
        "median_seconds": seconds[len(seconds) // 2] if seconds else None,
        "extra_items": totals["items"] - totals["mapped"] - totals["conflicted"],
        **totals,
    }


def beats(candidate: Mapping[str, float], baseline: Mapping[str, float]) -> bool:
    """The README's admission rule: higher F1, no lower precision, valid_rate >= 95%."""
    return (
        not candidate.get("inconclusive")
        and candidate["f1"] > baseline["f1"]
        and candidate["precision"] >= baseline["precision"]
        and candidate["valid_rate"] >= MIN_VALID_RATE
    )


def freeze(state_dir: Path, per_provider: int, version: int, split: str = "main") -> None:
    """Freeze episodes that have provider-supplied chapters and a usable agenda (read-only).

    Selection reuses the locator research cohort (``collect_benchmark_cohort`` +
    ``select_locator_samples``: provider x duration x body diverse, deterministic) and its
    chapter-validity and agenda-artifact checks, without the locator's body-disjoint split.
    """
    import tempfile

    from audit_chapters import collect_benchmark_cohort
    from build_locator_benchmark import select_locator_samples
    from build_locator_dataset import _artifact_class, _fetch_agenda_text, _is_valid_chapter_record

    from citypods.config import load_city_configs, load_site_config
    from citypods.http import make_session
    from citypods.storage.s3 import b2_from_env

    site = load_site_config("config/site_config.yml")
    cities = load_city_configs("config", site.get("defaults", {}))
    cohort = collect_benchmark_cohort(cities, state_dir, sample_size=999_999)
    # Every other split's episodes are excluded, so the splits never share a meeting.
    excluded = {
        episode["uid"]
        for other, directory in SPLIT_DIRS.items()
        if other != split and (directory / "manifest.json").exists()
        for episode in _load("manifest.json", other)["episodes"]
    }
    pool = select_locator_samples(cohort, per_provider=per_provider * 3 + len(excluded))
    session, storage = make_session(), b2_from_env()
    episodes, gold, skipped = [], [], []
    with tempfile.TemporaryDirectory() as tmp:
        for provider in sorted(pool):
            kept = 0
            for sample in pool[provider]:
                if kept >= per_provider:
                    break
                if sample.uid in excluded:
                    continue
                ok, reason = _is_valid_chapter_record(sample)
                if not ok:
                    skipped.append({"uid": sample.uid, "reason": reason})
                    continue
                data, _source, error = _fetch_agenda_text(
                    sample, session=session, storage=storage, temporary_dir=Path(tmp)
                )
                text = (data or b"").decode("utf-8", errors="replace")
                artifact_class = _artifact_class(text) if data else "unavailable"
                if artifact_class != "complete":
                    skipped.append({"uid": sample.uid, "reason": error or artifact_class})
                    continue
                episodes.append(
                    {
                        "uid": sample.uid,
                        "slug": sample.slug,
                        "provider": provider,
                        "body": sample.body,
                        "published": sample.published,
                        "agenda_text": text,
                    }
                )
                gold.append(
                    {
                        "uid": sample.uid,
                        "chapters": [
                            {"title": title, "start": start}
                            for title, start in zip(
                                sample.canonical_titles, sample.canonical_starts, strict=True
                            )
                        ],
                    }
                )
                kept += 1
    stamp = datetime.now(UTC).date().isoformat()
    common = {"version": version, "frozen_on": stamp, "per_provider": per_provider, "split": split}
    out_dir = SPLIT_DIRS[split]
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "manifest.json").write_text(
        json.dumps({**common, "skipped": skipped, "episodes": episodes}, indent=1) + "\n",
        encoding="utf-8",
    )
    (out_dir / "gold.json").write_text(
        json.dumps({**common, "episodes": gold}, indent=1) + "\n", encoding="utf-8"
    )
    counts = {p: sum(e["provider"] == p for e in episodes) for p in sorted(pool)}
    print(f"froze {len(episodes)} episodes {counts}; skipped {len(skipped)}")


def rescore(results: Mapping[str, Any]) -> dict[str, Any]:
    """Re-finalize every stored reply with the current validator; provider outcomes are kept.

    Only replies are re-judged: unanswered episodes stay unanswered, so a before/after comparison
    isolates the validator change from model and provider variance.
    """
    split = results.get("split", "main")
    manifest, gold = _load("manifest.json", split), _load("gold.json", split)
    episodes = {episode["uid"]: episode for episode in manifest["episodes"]}
    out = {**results, "rescored_at": datetime.now(UTC).isoformat(timespec="seconds"), "models": {}}
    for model, entry in results["models"].items():
        runs = []
        for run in entry["episodes"]:
            content = run.get("raw_response")
            if content is None:
                content = (run.get("validation") or {}).get("raw_response")
            if not run.get("answered") or content is None:
                runs.append(run)
                continue
            runs.append(
                {
                    **finalize_reply(episodes[run["uid"]], content, model),
                    "seconds": run.get("seconds"),
                }
            )
        summary = {**entry["summary"], **summarize(runs, gold)}
        summary["rescorable"] = sum(1 for r in runs if r.get("raw_response") is not None)
        out["models"][model] = {"summary": summary, "episodes": runs}
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--model", action="append", default=[])
    parser.add_argument("--freeze", action="store_true")
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--per-provider", type=int, default=6)
    parser.add_argument("--set-version", type=int, default=1)
    parser.add_argument("--workers", type=int, default=3, help="concurrent episodes per model")
    parser.add_argument("--no-pause", action="store_true")
    parser.add_argument("--out", type=Path, help="results JSON (default: evals/.../results/)")
    parser.add_argument("--split", choices=sorted(SPLIT_DIRS), default="main")
    parser.add_argument(
        "--rescore",
        type=Path,
        help="re-finalize a results file's stored replies with the current code (no model calls)",
    )
    args = parser.parse_args(argv)
    # A terminated run must still resume the providers it paused: turn SIGTERM into SystemExit so
    # the `paused(...)` contexts' finally blocks run (their Worker-side expiry is the backstop).
    import signal

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    if args.freeze:
        if not args.state_dir:
            parser.error("--freeze needs --state-dir")
        freeze(args.state_dir, args.per_provider, args.set_version, args.split)
        return 0
    if args.rescore:
        rescored = rescore(json.loads(args.rescore.read_text(encoding="utf-8")))
        out = args.out or args.rescore.with_name(args.rescore.stem + "-rescored.json")
        out.write_text(json.dumps(rescored, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        for model, entry in rescored["models"].items():
            print(json.dumps({"model": model, **entry["summary"]}, sort_keys=True))
        print(f"wrote {out}")
        return 0
    if not args.model:
        parser.error("give at least one --model")

    from citypods.compute.llm import LiteLLMBackend, LLMBackendConfig
    from citypods.compute.llm_dispatch_pause import Selection, paused
    from citypods.compute.llm_policy import ROUTE_CANDIDATES, canonical_model

    manifest, gold = _load("manifest.json", args.split), _load("gold.json", args.split)
    results: dict[str, Any] = {
        "task": "chapter-agenda",
        "split": args.split,
        "eval_set_version": manifest["version"],
        "run_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "models": {},
    }
    for model in args.model:
        routes = ROUTE_CANDIDATES.get(canonical_model(model)) or ()
        providers = sorted({r.provider for r in routes})
        # Production runs this task through the Worker, whose response ceiling is 720 s; the
        # direct client's 30 s default would time out long agendas and retry them repeatedly.
        backend = LiteLLMBackend(
            replace(
                LLMBackendConfig.from_env(),
                model=model,
                mode="direct",
                timeout_seconds=WORKER_RESPONSE_SECONDS,
            )
        )

        def run_all(backend=backend, model=model) -> list[dict]:
            with ThreadPoolExecutor(args.workers) as ex:
                return list(ex.map(lambda e: _run_one(backend, model, e), manifest["episodes"]))

        if args.no_pause:
            runs, contended = run_all(), True
        else:
            # Pause every provider serving this model so production traffic cannot distort it.
            from contextlib import ExitStack

            with ExitStack() as stack:
                outcomes = [
                    stack.enter_context(
                        paused(
                            Selection("provider", provider),
                            seconds=3600,
                            drain_timeout=600,
                            reason="chapter-agenda eval",
                        )
                    )
                    for provider in providers
                ]
                runs = run_all()
            contended = any(outcome.contended for outcome in outcomes)
        summary = summarize(runs, gold)
        summary["providers"] = providers
        summary["contended"] = contended
        results["models"][model] = {"summary": summary, "episodes": runs}
        print(json.dumps({"model": model, **summary}, sort_keys=True), flush=True)
    suffix = "" if args.split == "main" else f"-{args.split}"
    out = args.out or EVAL_DIR / "results" / f"{datetime.now(UTC):%Y-%m-%d}{suffix}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    existing = json.loads(out.read_text()) if out.exists() else {"models": {}}
    existing.update({k: v for k, v in results.items() if k != "models"})
    existing["models"] = {**existing.get("models", {}), **results["models"]}
    out.write_text(json.dumps(existing, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
