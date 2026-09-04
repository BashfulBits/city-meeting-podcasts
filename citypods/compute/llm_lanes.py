"""Canonical registry of LLM dispatch lanes, loaded from ``site_config.yml``'s ``llm_lanes``.

One entry per ``LLMRequestPolicy.purpose`` that can reach the Cloudflare Dispatch v2 ingress
Worker. This module is the client half of that contract; ``scripts/compile_llm_lanes.py`` compiles
the same block into the Worker's ``ingress_reservations.json`` so both halves can never disagree
about which purposes exist or what each may spend.

Two failure modes this replaces, both silent before:

* **Model choice split across config and code.** ``tagging.llm_models``/``moments.llm_models`` were
  config while the chapter, tournament, and R5-benchmark routes were Python constants, so no single
  place showed what the catalog dispatches to and a route change meant a code change.
* **Reservation keys that matched no real purpose.** The Worker's hand-maintained
  ``INGRESS_PURPOSE_RESERVATIONS`` reserved write units under ``topic-tags`` and ``moments`` while
  the client sent ``topic-tags:tagger``, ``topic-tags:prelabeler``, ``r6-moments``, and
  ``r6-judge`` -- so 10,000 daily write units were withheld from every other lane on behalf of two
  lanes that could never spend them.

An unregistered purpose is an error here, never a fallback: :func:`lane_for` raises
:class:`UnregisteredLaneError` rather than inventing a model list or letting the job through to the
Worker's shared headroom. Adding a new verb/task therefore requires a new ``llm_lanes`` entry, which
is the point -- see the block comment in ``config/site_config.yml``.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Anchored to the repository root rather than the process CWD. Several lanes resolve their models
# at import time to build a recipe hash, so a lookup that silently found no config (or a different
# one) because a tool ran from a subdirectory would change recipe hashes and re-queue the catalog.
# An explicit `path=`/`site_config=` argument still overrides this.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_SITE_CONFIG_PATH = _REPO_ROOT / "config" / "site_config.yml"

# Purposes that deliberately have no lane entry because they never reach the ingress Worker.
# `topic-tags:rules` is rule-engine bookkeeping recorded with a `rule:<version>` pseudo-model and
# makes no LLM call at all; `city-onboarding` sets `require_direct=True` (review/41) so R12 city
# discovery calls the provider from the runner. Listed explicitly -- rather than simply omitted --
# so `tests/test_llm_lanes.py` can assert the split stays true if either ever starts dispatching.
NON_DISPATCHING_PURPOSES = frozenset({"topic-tags:rules", "city-onboarding"})


class UnregisteredLaneError(LookupError):
    """A dispatching purpose has no ``llm_lanes`` entry.

    Deliberately loud. The whole point of the registry is that a new verb/task cannot quietly
    start spending ingress write units that another lane was relying on.
    """


@dataclass(frozen=True)
class LaneConfig:
    """One registered dispatch lane."""

    purpose: str
    models: tuple[str, ...]
    max_dispatches_per_run: int
    reserved_write_units: int
    daily_write_units: int
    # How ``models`` maps onto jobs, which decides what one job costs at ingress:
    #
    # ``pooled`` (default) -- one job carries the whole list as its ``allowed_models`` and the
    #   scheduler picks whichever route has capacity. Extra routes are pure throughput. This is
    #   the production lanes' shape (tags, moments, chapters).
    # ``per_model`` -- the caller fans out one job per model, each pinned to a single route,
    #   because the models are being *compared*, not pooled. This is the research lanes' shape
    #   (tournament contestants, R5 benchmark taggers). Pooling them would let the scheduler
    #   answer a "how does model X tag this?" job with model Y and silently invalidate the
    #   comparison.
    #
    # The distinction is not cosmetic: a pooled job writes one model-index row per allowed model
    # while a per-model job writes exactly one, so charging a four-model per_model lane as if each
    # job indexed four routes would over-reserve its budget by ~75%.
    dispatch_shape: str = "pooled"

    @property
    def primary_model(self) -> str:
        """The stable recipe/calibration route.

        Every lane's recipe hash and calibration-matrix key is built from this one string, so it
        must stay stable even as throughput routes are added after it. ``additional_models``
        carries the rest.
        """
        return self.models[0]

    @property
    def additional_models(self) -> tuple[str, ...]:
        """Extra routes a lane may spill onto once ``primary_model``'s own quota window fills.

        These draw on independent free-tier pools; they are throughput, not a model comparison
        (that stays the tournament's job, review/34 §7).
        """
        return self.models[1:]

    @property
    def ingress_write_units_per_job(self) -> int:
        """The Worker's pessimistic per-job admission charge for this lane.

        Mirrors ``coordinator.js``'s ``_ingressWriteUnitsFor``: the job row, one model-index row
        per canonical allowed model, the purpose ledger, and the scheduler counter. Kept here so
        `compile_llm_lanes.py` can check a lane's budget actually admits a whole number of jobs
        instead of stranding a remainder.

        A ``per_model`` lane pins each job to one route, so it indexes one model however many
        routes the lane compares across.
        """
        return 3 + (1 if self.dispatch_shape == "per_model" else len(self.models))

    @property
    def max_jobs_per_day(self) -> int:
        """Whole jobs this lane's own daily budget admits, ignoring shared headroom.

        The floor matters: a budget that admits 999.75 jobs admits 999, and the remainder is
        stranded rather than lent to another lane.
        """
        return self.daily_write_units // self.ingress_write_units_per_job


def _coerce_int(raw: Any, *, purpose: str, field: str) -> int:
    """Read one non-negative integer budget field, naming the lane and field on failure.

    ``bool`` is rejected explicitly because it is an ``int`` subclass in Python, so a stray
    ``daily_write_units: true`` would otherwise silently become a budget of 1.
    """
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError(f"llm_lanes[{purpose!r}].{field} must be an integer, got {raw!r}")
    if raw < 0:
        raise ValueError(f"llm_lanes[{purpose!r}].{field} must be non-negative, got {raw}")
    return raw


def parse_lanes(raw_block: Any) -> dict[str, LaneConfig]:
    """Validate and normalize a raw ``llm_lanes`` mapping.

    Split from :func:`load_lanes` so the compiler and the tests can validate a block without
    touching the filesystem or the module-level cache.
    """
    if not isinstance(raw_block, Mapping) or not raw_block:
        raise ValueError("site config must define a non-empty 'llm_lanes' block")

    lanes: dict[str, LaneConfig] = {}
    for purpose, entry in raw_block.items():
        purpose = str(purpose)
        if purpose in NON_DISPATCHING_PURPOSES:
            raise ValueError(
                f"llm_lanes[{purpose!r}] is registered but that purpose never reaches the "
                "ingress Worker; remove the entry or drop it from NON_DISPATCHING_PURPOSES"
            )
        if not isinstance(entry, Mapping):
            raise ValueError(f"llm_lanes[{purpose!r}] must be a mapping")
        raw_models = entry.get("models")
        if not isinstance(raw_models, (list, tuple)) or not raw_models:
            raise ValueError(f"llm_lanes[{purpose!r}].models must be a non-empty list")
        models = tuple(str(model) for model in raw_models)
        if len(set(models)) != len(models):
            raise ValueError(f"llm_lanes[{purpose!r}].models contains duplicates: {models}")

        reserved = _coerce_int(
            entry.get("reserved_write_units", 0), purpose=purpose, field="reserved_write_units"
        )
        daily = _coerce_int(
            entry.get("daily_write_units"), purpose=purpose, field="daily_write_units"
        )
        if reserved > daily:
            raise ValueError(
                f"llm_lanes[{purpose!r}] reserves {reserved} write units but caps its day at "
                f"{daily}; a reservation it can never spend strands capacity from every other lane"
            )
        shape = entry.get("dispatch_shape", "pooled")
        if shape not in {"pooled", "per_model"}:
            raise ValueError(
                f"llm_lanes[{purpose!r}].dispatch_shape must be 'pooled' or 'per_model', "
                f"got {shape!r}"
            )
        lane = LaneConfig(
            purpose=purpose,
            models=models,
            max_dispatches_per_run=_coerce_int(
                entry.get("max_dispatches_per_run"),
                purpose=purpose,
                field="max_dispatches_per_run",
            ),
            reserved_write_units=reserved,
            daily_write_units=daily,
            dispatch_shape=shape,
        )
        if daily < lane.ingress_write_units_per_job:
            raise ValueError(
                f"llm_lanes[{purpose!r}].daily_write_units ({daily}) is below the "
                f"{lane.ingress_write_units_per_job} units one of its jobs costs, so the lane can "
                "never admit a single job"
            )
        if lane.max_dispatches_per_run > lane.max_jobs_per_day:
            # A run cap above what the daily budget funds is not a harmless upper bound: the
            # producer submits to that cap and the Worker rejects everything past the budget, so
            # the surplus is pure wasted ingress traffic that looks like a lane failing to fill
            # its quota. Every lane here is a daily cron, so the two numbers should agree.
            raise ValueError(
                f"llm_lanes[{purpose!r}].max_dispatches_per_run "
                f"({lane.max_dispatches_per_run}) exceeds the {lane.max_jobs_per_day} jobs its "
                f"daily_write_units ({daily}) can fund at {lane.ingress_write_units_per_job} "
                "units/job; the surplus would be rejected at ingress"
            )
        lanes[purpose] = lane
    return lanes


_CACHE: dict[str, dict[str, LaneConfig]] = {}
_CACHE_LOCK = threading.Lock()


def load_lanes(
    site_config: Mapping[str, Any] | None = None,
    *,
    path: str | Path = DEFAULT_SITE_CONFIG_PATH,
) -> dict[str, LaneConfig]:
    """Return the registry, from an already-loaded site config or by reading ``path``.

    A ``site_config`` that declares ``llm_lanes`` wins; one that omits the key falls back to the
    committed registry at ``path``. The registry is repository-level policy -- which dispatch
    purposes exist and what each may spend against a shared Cloudflare budget -- not per-deployment
    site content like ``custom_domain`` or ``tagging.enabled``. ``_build_impl`` is routinely handed
    a small synthetic config (tests, local dev, a single-city deployment), and before this block
    existed those callers got the models from Python constants without declaring anything; making
    the key mandatory would have turned every such config into a hard failure for policy it has no
    business restating.

    The fallback is deliberately narrow: it applies only when the key is ABSENT. A config that
    declares ``llm_lanes`` and gets it wrong still raises, so a real misconfiguration is never
    quietly replaced by the committed defaults.

    Results are cached per resolved path because every per-episode stage call asks for its lane;
    an explicit ``site_config`` carrying its own lanes bypasses the cache.
    """
    if site_config is not None and site_config.get("llm_lanes") is not None:
        return parse_lanes(site_config.get("llm_lanes"))

    key = str(Path(path))
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached is not None:
            return cached

    import yaml

    raw = yaml.safe_load(Path(key).read_text()) or {}
    lanes = parse_lanes(raw.get("llm_lanes"))
    with _CACHE_LOCK:
        _CACHE[key] = lanes
    return lanes


def lane_for(
    purpose: str,
    site_config: Mapping[str, Any] | None = None,
    *,
    path: str | Path = DEFAULT_SITE_CONFIG_PATH,
) -> LaneConfig:
    """Return the lane for ``purpose``, raising :class:`UnregisteredLaneError` if there is none.

    This is the only supported way for a call site to learn its models. Callers must not fall back
    to a hard-coded route when this raises: a lane the Worker will reject at ingress
    (``purpose_not_registered``) should fail here, in the producer, where the error names the
    config file to edit.

    ``site_config`` is consulted only when it declares its own ``llm_lanes``; otherwise the
    committed registry is used. See :func:`load_lanes` for why.
    """
    lanes = load_lanes(site_config, path=path)
    try:
        return lanes[purpose]
    except KeyError:
        if purpose in NON_DISPATCHING_PURPOSES:
            raise UnregisteredLaneError(
                f"purpose {purpose!r} is registered as non-dispatching and must not be queued to "
                "the dispatch Worker; it calls its provider directly or makes no LLM call at all"
            ) from None
        raise UnregisteredLaneError(
            f"purpose {purpose!r} has no 'llm_lanes' entry in the site config. Every dispatching "
            "purpose needs its own entry naming its models and its ingress write budget -- a new "
            "verb or task does not inherit another lane's budget from a shared prefix. Add it to "
            f"config/site_config.yml and recompile with scripts/compile_llm_lanes.py. "
            f"Registered lanes: {sorted(lanes)}"
        ) from None


def clear_cache() -> None:
    """Drop the parsed-config cache (tests that write temporary site configs)."""
    with _CACHE_LOCK:
        _CACHE.clear()


__all__ = [
    "DEFAULT_SITE_CONFIG_PATH",
    "NON_DISPATCHING_PURPOSES",
    "LaneConfig",
    "UnregisteredLaneError",
    "clear_cache",
    "lane_for",
    "load_lanes",
    "parse_lanes",
]
