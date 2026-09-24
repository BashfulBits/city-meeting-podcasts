"""Relative model quality from Artificial Analysis (AA): informational, never a gate.

One AA fetch per run. A model is matched by exact identity only -- normalized creator + model
slug -- with generic naming rules instead of hand-kept per-model alias tables: AA may prefix the
creator to the slug, and release-channel/variant suffixes (`-it`, `-instruct`, `-chat`, `-preview`,
`-reasoning`) are optional on either side. An exact slug always wins; a suffix-normalized match is
used only when exactly one AA model normalizes to it, so ambiguity stays unscored. A model AA
does not know is "unscored", and the issue offers research links rather than a guessed score. The
floor
(the lower of GPT-OSS-120B and Nemotron-3 Super) is shown as a flag; lanes, not this module,
decide what is good enough.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import requests

AA_URL = "https://artificialanalysis.ai/api/v2/data/llms/models"
AA_KEY_ENV = "ARTIFICIAL_ANALYSIS_API_KEY"
AA_METRIC = "artificial_analysis_intelligence_index"
FLOOR_MODELS = (("openai", "gpt-oss-120b"), ("nvidia", "nvidia-nemotron-3-super-120b-a12b"))
# Publisher namespaces as providers spell them -> AA's creator slug. Publisher identity, not
# per-model aliases; extend only when a publisher is spelled differently on the two sides.
CREATOR_ALIASES = {
    "deepseek-ai": "deepseek",
    "meta-llama": "meta",
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


def _strip_optional(slug: str) -> str:
    while (stripped := _OPTIONAL_SUFFIX.sub("", slug)) != slug:
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
    try:
        response = session.get(AA_URL, headers={"x-api-key": api_key}, timeout=30)
        if not response.ok:
            return QualityIndex(error=f"Artificial Analysis HTTP {response.status_code}")
        rows = response.json().get("data") or []
    except (requests.RequestException, ValueError, AttributeError) as exc:
        return QualityIndex(error=f"Artificial Analysis {type(exc).__name__}")
    return QualityIndex(scores=_scores(rows))


def _scores(rows: list[Mapping[str, Any]]) -> dict[tuple[str, str], float]:
    scores: dict[tuple[str, str], float] = {}
    for row in rows:
        creator = (row.get("model_creator") or {}).get("slug")
        slug = row.get("slug")
        try:
            value = float((row.get("evaluations") or {})[AA_METRIC])
        except (KeyError, TypeError, ValueError):
            continue
        if isinstance(creator, str) and isinstance(slug, str) and value >= 0:
            scores[(creator.lower(), slug.lower())] = value
    return scores
