"""Automated resolution of unexpected-body audit findings using LLM reasoning.

Gathers un-truncated municipal context and historical records, executes LLM classification
via LiteLLMBackend (routing through Cloudflare AI Gateway with atomic R2 CAS rate reservations),
enforces the Joint Meeting dual-body inclusion rule, applies YAML mutations, and validates
changes with ruff and pytest.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from citypods.compute.base import InferenceJob, JobHandle
from citypods.compute.llm import LiteLLMBackend, LLMBackendConfig
from citypods.compute.llm_policy import LLMRequestPolicy
from citypods.models import City, Episode


class YAMLMutation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_path: str = Field(description="Relative path to config/feeds/*.yml")
    action: Literal["add_body_any", "add_body_includes", "create_feed"] = Field(
        description="Type of mutation to apply"
    )
    content: dict[str, Any] = Field(description="Payload to merge or full feed content to write")


class BodyClassification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_key: str
    unexpected_body: str
    action: Literal["union", "single_uid_inclusion", "new_feed"]
    target_feeds: list[str] = Field(
        description="Target feed slug(s). If this is a Joint meeting between two bodies, list both."
    )
    rationale: str = Field(
        description="Concise rationale citing dates, frequency, and municipal taxonomy."
    )
    mutations: list[YAMLMutation] = Field(description="YAML mutations to apply for this finding.")


class RemedyOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    classifications: list[BodyClassification]


REMEDY_TASK_PROMPT = """You are an expert municipal media data engineer managing city podcasts.
An automated audit detected meeting recordings with labels not matched by existing feed filters.

Classify each finding into:
1. "union": Alternate label/series for an existing feed (adds to `body_any`).
2. "single_uid_inclusion": One-off event/typo/special luncheon/date (adds to `body_includes`).
3. "new_feed": Recurring distinct board/commission (creates a new feed YAML).

JOINT MEETING RULE:
For meetings with 'Joint', 'and', '/', or 'with' in their name or body:
- If BOTH bodies exist as configured feeds, list BOTH feed slugs in `target_feeds`.
- If only ONE body is configured, attach to that feed and note the unconfigured body in rationale.

EVIDENCE DATASET:
{evidence_json}
"""


def gather_unexpected_body_evidence(
    source_key: str,
    city_slug: str,
    unmatched_episodes: list[Episode],
    related_cities: list[City],
    records: dict[str, Any],
    repo_root: str | Path = ".",
) -> dict[str, Any]:
    """Compile un-truncated municipal and historical evidence for LLM classification."""
    root = Path(repo_root)
    city_yaml_path = root / "config" / "cities" / f"{city_slug}.yml"
    city_info: dict[str, Any] = {}
    if city_yaml_path.exists():
        city_info = yaml.safe_load(city_yaml_path.read_text(encoding="utf-8")) or {}

    existing_feeds = []
    for feed in related_cities:
        src = feed.source if isinstance(feed.source, dict) else {}
        existing_feeds.append(
            {
                "slug": feed.slug,
                "podcast_title": feed.podcast_title,
                "podcast_description": getattr(feed, "podcast_description", ""),
                "body": src.get("body"),
                "body_any": src.get("body_any", []),
                "body_includes": src.get("body_includes", []),
            }
        )

    archived_bodies = list({rec.get("body") for rec in records.values() if rec.get("body")})
    sample_titles = [rec.get("title") for rec in list(records.values())[:10] if rec.get("title")]

    grouped: dict[str, list[dict[str, Any]]] = {}
    for ep in unmatched_episodes:
        label = ep.body or "Unknown"
        grouped.setdefault(label, []).append(
            {
                "guid": ep.guid,
                "published": ep.published.isoformat() if ep.published else "",
                "title": ep.title,
                "body": ep.body,
            }
        )

    unexpected_findings = []
    for label, eps in grouped.items():
        dates = sorted([e["published"] for e in eps if e["published"]])
        unexpected_findings.append(
            {
                "unexpected_body": label,
                "count": len(eps),
                "date_range": {
                    "earliest": dates[0] if dates else "",
                    "latest": dates[-1] if dates else "",
                },
                "episodes": eps,
            }
        )

    return {
        "source_key": source_key,
        "city": {
            "slug": city_slug,
            "name": city_info.get("name", city_slug),
            "state": city_info.get("state", ""),
            "website": city_info.get("city_website", ""),
            "meetings_url": city_info.get("meetings_url", ""),
        },
        "existing_feeds": existing_feeds,
        "historical_archive": {
            "total_archived_count": len(records),
            "known_archived_bodies": archived_bodies,
            "sample_past_titles": sample_titles,
        },
        "unexpected_findings": unexpected_findings,
    }


def classify_unexpected_bodies(
    evidence_bundle: list[dict[str, Any]],
    storage: Any = None,
    *,
    backend: LiteLLMBackend | None = None,
) -> RemedyOutput:
    """Query LLM routes with preferential free tier selection and 429 failover."""
    policy = LLMRequestPolicy(
        allow_paid=False,
        allowed_models=(
            "gemini-3.7-flash",
            "gemini-3.6-flash",
            "gemini-3.1-pro",
            "deepseek-v4-flash-free",
        ),
        timeout_class="batch",
        deadline_at=datetime.now(UTC) + timedelta(minutes=5),
    )

    prompt = REMEDY_TASK_PROMPT.format(evidence_json=json.dumps(evidence_bundle, indent=2))

    if backend is None:
        backend = LiteLLMBackend(
            LLMBackendConfig(
                model="gemini/gemini-3.7-flash",
                additional_models=(
                    "gemini/gemini-3.6-flash",
                    "gemini/gemini-3.1-pro",
                    "opencode/deepseek-v4-flash-free",
                ),
            ),
            storage=storage,
        )

    job = InferenceJob(
        task="tag",
        inputs={
            "messages": [{"role": "user", "content": prompt}],
            "llm_policy": policy,
        },
        recipe_hash=f"unexpected-bodies-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}",
    )

    result = backend.run_inference(job)
    if isinstance(result, JobHandle):
        raise RuntimeError("Remedy LLM inference was deferred; no synchronous result available.")

    content = ""
    if isinstance(result.output, dict):
        choices = result.output.get("choices")
        if choices and isinstance(choices, list):
            content = choices[0].get("message", {}).get("content", "")

    if not content:
        raise ValueError("LLM returned empty remedy content")

    # Clean markdown json code blocks if present
    content = content.strip()
    if content.startswith("```json"):
        content = content[7:]
    elif content.startswith("```"):
        content = content[3:]
    if content.endswith("```"):
        content = content[:-3]

    return RemedyOutput.model_validate_json(content.strip())


def apply_remedy_mutations(remedy: RemedyOutput, repo_root: str | Path = ".") -> list[str]:
    """Apply YAML mutations to local files and return the list of modified paths."""
    root = Path(repo_root)
    modified_paths: list[str] = []

    for item in remedy.classifications:
        for mut in item.mutations:
            path = root / mut.file_path
            path.parent.mkdir(parents=True, exist_ok=True)
            modified_paths.append(str(path))

            if mut.action == "create_feed":
                with open(path, "w", encoding="utf-8") as f:
                    yaml.safe_dump(mut.content, f, sort_keys=False)
            elif mut.action in ("add_body_any", "add_body_includes"):
                data: dict[str, Any] = {}
                if path.exists():
                    with open(path, encoding="utf-8") as f:
                        data = yaml.safe_load(f) or {}

                src = data.setdefault("source", {})
                if mut.action == "add_body_any":
                    any_list = src.setdefault("body_any", [])
                    for v in mut.content.get("body_any", []):
                        if v not in any_list:
                            any_list.append(v)
                elif mut.action == "add_body_includes":
                    inc_list = src.setdefault("body_includes", [])
                    for inc in mut.content.get("body_includes", []):
                        if not any(
                            x.get("provider_guid") == inc.get("provider_guid") for x in inc_list
                        ):
                            inc_list.append(inc)

                with open(path, "w", encoding="utf-8") as f:
                    yaml.safe_dump(data, f, sort_keys=False)

    return list(dict.fromkeys(modified_paths))


def verify_remedy_mutations(repo_root: str | Path = ".") -> tuple[bool, str]:
    """Run ruff and pytest to verify that mutations build cleanly and pass lint."""
    r_lint = subprocess.run(["ruff", "check", "."], cwd=repo_root, capture_output=True, text=True)
    if r_lint.returncode != 0:
        return False, f"Ruff lint failed:\n{r_lint.stdout}\n{r_lint.stderr}"

    r_test = subprocess.run(
        ["pytest", "-q", "tests/test_audit.py"], cwd=repo_root, capture_output=True, text=True
    )
    if r_test.returncode != 0:
        return False, f"Pytest failed:\n{r_test.stdout}\n{r_test.stderr}"

    return True, "All verification checks passed cleanly."


def format_remedy_markdown_table(remedy: RemedyOutput) -> str:
    """Format remedy findings and classifications as a markdown table."""
    lines = [
        "| Source | Unexpected Body | Action | Target Feed(s) | Rationale |",
        "|---|---|---|---|---|",
    ]
    for c in remedy.classifications:
        feeds_str = ", ".join(f"`{slug}`" for slug in c.target_feeds)
        lines.append(
            f"| `{c.source_key}` | `{c.unexpected_body}` | **{c.action}** | "
            f"{feeds_str} | {c.rationale} |"
        )
    return "\n".join(lines)
