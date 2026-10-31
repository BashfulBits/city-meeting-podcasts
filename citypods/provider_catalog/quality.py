"""Relative model quality from Artificial Analysis (AA): informational, never a gate.

One paginated AA catalog fetch per run. A model is matched by exact identity only -- normalized
creator + model slug -- with generic naming rules instead of hand-kept per-model alias tables:
AA may prefix the creator to the slug, and release-channel/variant suffixes (`-it`, `-instruct`,
`-chat`, `-preview`, `-reasoning`) are optional on either side. An exact slug always wins; a
suffix-normalized match is used only when exactly one AA model normalizes to it, so ambiguity
stays unscored. A model AA does not know is "unscored", and the issue offers research links
rather than a guessed score. The floor (the lower of GPT-OSS-120B and Nemotron-3 Super) is shown
as a flag; lanes, not this module, decide what is good enough.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import requests

# AA's API reference (checked 2026-11-01) documents one free endpoint for language models:
# `GET /api/v2/data/llms/models`, `x-api-key` header, 1,000 requests/day, no parameters and no
# pagination, response `{status, prompt_options, data: [{id, name, slug, model_creator: {id, name,
# slug}, evaluations: {artificial_analysis_intelligence_index: number|null, ...}, pricing, ...}]}`.
# Attribution is required (the issue header links AA) and responses should be cached (one fetch per
# run). The earlier paginated `/language/models/free` path is kept as the fallback for an HTTP
# error or empty result in case a key still serves only that; malformed data is never retried
# elsewhere. Failures are reported (the issue header shows them) instead of silently leaving every
# model unscored.
AA_URLS = (
    "https://artificialanalysis.ai/api/v2/data/llms/models",
    "https://artificialanalysis.ai/api/v2/language/models/free",
)
AA_URL = AA_URLS[0]
_PAGINATED_URL = AA_URLS[1]
AA_KEY_ENV = "ARTIFICIAL_ANALYSIS_API_KEY"
AA_METRIC = "artificial_analysis_intelligence_index"
FLOOR_MODELS = (("openai", "gpt-oss-120b"), ("nvidia", "nvidia-nemotron-3-super-120b-a12b"))
# Publisher namespaces as providers spell them -> AA's creator slug. Publisher identity, not
# per-model aliases; extend only when a publisher is spelled differently on the two sides.
CREATOR_ALIASES = {
    "deepseek-ai": "deepseek",
    "meta-llama": "meta",
    "minimaxai": "minimax",
    "mistralai": "mistral",
    "moonshotai": "kimi",
    "moonshot": "kimi",
    "qwen": "alibaba",
    "z-ai": "zai",
    "zai-org": "zai",
}
# Release-channel/variant markers that name the same evaluated model on one side only
# (backtest 2026-09-24: gemini-3.1-flash-lite ~ AA gemini-3-1-flash-lite-preview,
# gemini-3-flash-preview ~ AA gemini-3-flash, nemotron-3-nano-omni-...-reasoning ~ AA ...-a3b).
_OPTIONAL_SUFFIX = re.compile(r"-(it|instruct|chat|preview|reasoning)$")
# A mixture-of-experts size tag (`-1t-a52b`, `-30b-a3b`) restates the model, it does not name a
# different one; the active-parameter half makes a collision between two models implausible.
_SIZE_SUFFIX = re.compile(r"-\d+[bt]-a\d+b$")


def _strip_optional(slug: str) -> str:
    while (stripped := _SIZE_SUFFIX.sub("", _OPTIONAL_SUFFIX.sub("", slug))) != slug:
        slug = stripped
    return slug


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


@dataclass
class QualityIndex:
    scores: dict[tuple[str, str], float] = field(default_factory=dict)
    error: str | None = None
    _by_normalized: dict[tuple[str, str], list[float]] | None = field(
        default=None, repr=False, compare=False
    )

    @property
    def floor(self) -> float | None:
        values = [self.scores.get(identity) for identity in FLOOR_MODELS]
        return None if any(v is None for v in values) else min(values)  # type: ignore[type-var]

    def score(
        self, model: str, *, creator: str | None = None, free_suffix: str = ""
    ) -> float | None:
        """AA Intelligence Index for a provider model ID, or None if AA has no single match."""
        model = model.removesuffix(free_suffix) if free_suffix else model
        namespace, _, name = model.rpartition("/")
        namespace = namespace.split("/")[-1].lower() if namespace else ""
        creators = [c for c in (CREATOR_ALIASES.get(namespace, namespace), creator) if c]
        slug = _slug(name)
        for candidate_creator in creators:
            # AA sometimes prefixes the creator (`nvidia-nemotron-3-super-...`).
            variants = (slug, f"{candidate_creator}-{slug}")
            for variant in variants:
                exact = self.scores.get((candidate_creator, variant))
                if exact is not None:
                    return exact
            for variant in variants:
                matches = self._normalized().get((candidate_creator, _strip_optional(variant)))
                if matches is not None and len(matches) == 1:
                    return matches[0]
        if not creators:
            # A bare ID from a multi-publisher host (SambaNova's `gemma-4-31B-it`): accept the AA
            # model with this normalized slug only when exactly one creator has one.
            matches = [
                values
                for (_creator, slug_key), values in self._normalized().items()
                if slug_key == _strip_optional(slug)
            ]
            if len(matches) == 1 and len(matches[0]) == 1:
                return matches[0][0]
        return None

    def _normalized(self) -> dict[tuple[str, str], list[float]]:
        if self._by_normalized is None:
            index: dict[tuple[str, str], list[float]] = {}
            for (creator, slug), value in self.scores.items():
                index.setdefault((creator, _strip_optional(slug)), []).append(value)
            self._by_normalized = index
        return self._by_normalized


def fetch_quality_index(session: requests.Session) -> QualityIndex:
    api_key = os.environ.get(AA_KEY_ENV)
    if not api_key:
        return QualityIndex(error=f"{AA_KEY_ENV} not set")
    errors = []
    for url in AA_URLS:
        index, moved = _fetch_one(session, url, api_key)
        if not index.error:
            return index
        errors.append(index.error)
        if not moved:
            break  # A failure after data arrived, or malformed data, is not a moved endpoint.
    return QualityIndex(error="; ".join(errors))


def _fetch_one(session: requests.Session, url: str, api_key: str) -> tuple[QualityIndex, bool]:
    """Return the index and whether a failure looks like a moved endpoint (first page, no data)."""
    path = url.split("artificialanalysis.ai", 1)[-1]
    scores: dict[tuple[str, str], float] = {}
    page = 1
    try:
        while True:
            # Only the legacy endpoint takes a page; the documented one takes no parameters.
            params = {"page": page} if url == _PAGINATED_URL else {}
            response = session.get(url, headers={"x-api-key": api_key}, params=params, timeout=30)
            if not response.ok:
                error = f"Artificial Analysis {path} HTTP {response.status_code}"
                return QualityIndex(error=error), page == 1
            payload = response.json()
            rows = payload["data"]
            if not isinstance(rows, list):
                return QualityIndex(error=f"Artificial Analysis {path} invalid data"), False
            scores.update(_scores(rows))
            pagination = payload.get("pagination")
            if pagination is None:
                break  # The documented endpoint returns the whole list in one response.
            total_pages = pagination["total_pages"]
            if (
                pagination["page"] != page
                or not isinstance(pagination["has_more"], bool)
                or type(total_pages) is not int
                or total_pages < page
                or pagination["has_more"] != (page < total_pages)
                or (pagination["has_more"] and not rows)
            ):
                return QualityIndex(error=f"Artificial Analysis {path} invalid pagination"), False
            if not pagination["has_more"]:
                break
            page += 1
    except (requests.RequestException, ValueError, AttributeError, KeyError, TypeError) as exc:
        error = f"Artificial Analysis {path} {type(exc).__name__}"
        return QualityIndex(error=error), page == 1 and isinstance(exc, requests.RequestException)
    if not scores:
        error = f"Artificial Analysis {path} listed no Intelligence Index scores"
        return QualityIndex(error=error), True
    return QualityIndex(scores=scores), False


def _scores(rows: list[Mapping[str, Any]]) -> dict[tuple[str, str], float]:
    scores: dict[tuple[str, str], float] = {}
    for row in rows:
        creator_row = row.get("model_creator") or {}
        creator_name = creator_row.get("name")
        creator = _slug(creator_name) if isinstance(creator_name, str) else ""
        creator_slug = creator_row.get("slug")
        # V2 exposes publisher names, not the legacy creator slug.
        creator = {
            "mistral-ai": "mistral",
            "moonshot-ai": "kimi",
            "z-ai": "zai",
            "x-ai": "xai",
        }.get(creator, creator)
        slug = row.get("slug")
        try:
            value = float((row.get("evaluations") or {})[AA_METRIC])
        except (KeyError, TypeError, ValueError):
            continue
        if isinstance(creator_slug, str) and _slug(creator_slug):
            # AA documents the creator slug as the stable identifier; the display name is an alias.
            keys = {_slug(creator_slug), creator} - {""}
        else:
            keys = {creator} - {""}
        if keys and isinstance(slug, str) and value >= 0:
            for key in keys:
                scores[(key.lower(), slug.lower())] = value
    return scores
