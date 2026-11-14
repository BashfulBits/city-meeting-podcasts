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

import math
import threading
from collections.abc import Mapping
from dataclasses import dataclass, replace
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
    max_dispatches_per_run: int | None = None
    reserved_write_units: int = 0
    daily_write_units: int = 0
    max_queued_jobs: int | None = None
    # review/58 section 12.11: this lane's share of the daily row budget. When set, the three caps
    # above are computed by :func:`allocate_reservations` instead of being stated in config.
    reservation_ratio: float | None = None
    # review/58 section 12.11: a live lane (a probe on live calls) splits the fixed
    # `llm_lane_budget.live_reservation_jobs` pool with the other live lanes, by ratio, instead of
    # a share of the row budget.
    live: bool = False
    telemetry_producer: str = ""
    telemetry_unit: str = "episode"
    telemetry_completion: str = "consumed"
    telemetry_scope: str = "retained_catalog"
    # How ``models`` maps onto jobs, which decides what one job costs at ingress:
    #
    # ``pooled`` (default) -- one job carries the whole list as its ``allowed_models`` and the
    #   scheduler picks whichever route has capacity. Extra routes are pure throughput. This is
    #   the production lanes' shape (tags, moments, chapters).
    # ``per_model`` -- each job is pinned to one allowed model and indexes only that model. This
    #   is used for model comparisons and for production policies that select one model per request
    #   (for example, size-based routing). It must not be pooled.
    #
    # The distinction is not cosmetic: a pooled job writes one model-index row per allowed model
    # while a per-model job writes exactly one, so charging a four-model per_model lane as if each
    # job indexed four routes would over-reserve its budget by ~75%.
    dispatch_shape: str = "pooled"
    # Models eligible only once a queued job has failed enough that ``models`` alone looks stuck --
    # see LLMRequestPolicy.backup_models/backup_after_attempts and workers/llm-dispatch-v2/src/
    # routes.js's backupModelsActive()/modelsForJob(). Worker (queue_only) dispatch only:
    # direct-mode calls have no persistent cross-run attempt counter to gate on today. Never part
    # of a job's indexed model set at enqueue time, so ingress_write_units_per_job stays keyed on
    # ``models``.
    backup_models: tuple[str, ...] = ()
    # How many times a job must have been dispatched without a successful response (Worker
    # ``jobs.attempts``) before backup_models become eligible -- or, independently, a job that has
    # already needed one JSON-schema-validation correction (``jobs.schema_retry_count >= 1``) is
    # eligible immediately, since that failure mode is a model-output problem, not a capacity one.
    # Required (and only meaningful) together with backup_models.
    backup_after_attempts: int | None = None
    # Whether the provider-catalog reconciler (review/48) may offer newly discovered models as
    # backup_models candidates for this lane. Default on, so a new lane is eligible with no edit;
    # set false for a lane whose model set must stay fixed. A ``per_model`` lane is never eligible
    # (it cannot declare backup_models at all) -- see ``accepts_catalog_backups``.
    catalog_backup_candidates: bool = True

    @property
    def accepts_catalog_backups(self) -> bool:
        return self.catalog_backup_candidates and self.dispatch_shape == "pooled"

    # Per-model reasoning level for THIS lane's jobs (e.g. ``{"deepseek/deepseek-v4.1-flash":
    # "off"}``), applied at send time through the route's ``reasoning_controls``. A model without
    # an entry keeps its provider default, so a model can think in one job type and not another.
    # Stored as sorted pairs to keep the frozen config hashable; see ``reasoning_levels``.
    reasoning: tuple[tuple[str, str], ...] = ()
    # Judge pools (review/53): models qualified to be substituted into ``models`` but never
    # dispatched while they are only eligible. Not part of the ingress map, so a job naming one is
    # rejected at ingress like any model outside the lane. A route league (review/49 P6) or a
    # maintainer moves a model between the two lists by config PR.
    eligible_models: tuple[str, ...] = ()
    # Named slots, each a tuple of models (active or eligible), e.g. the sibling judge's slot A
    # (Google) and slot B (non-Google). Stored as sorted pairs to keep the config hashable.
    slots: tuple[tuple[str, tuple[str, ...]], ...] = ()

    @property
    def slot_models(self) -> dict[str, tuple[str, ...]]:
        return dict(self.slots)

    @property
    def reasoning_levels(self) -> dict[str, str]:
        return dict(self.reasoning)

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


def _coerce_bool(raw: Any, *, purpose: str, field: str) -> bool:
    if not isinstance(raw, bool):
        raise ValueError(f"llm_lanes[{purpose!r}].{field} must be true or false, got {raw!r}")
    return raw


def _coerce_number(raw: Any, *, where: str, positive: bool = False) -> float:
    """Read one non-negative (or, with ``positive``, strictly positive) real number."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw != raw:
        raise ValueError(f"{where} must be a number, got {raw!r}")
    if raw < 0 or (positive and raw == 0):
        kind = "positive" if positive else "non-negative"
        raise ValueError(f"{where} must be {kind}, got {raw}")
    return float(raw)


# ---------------------------------------------------------------------------------------------
# Lane reservations split from one row budget by ratio (review/58 section 12.11).
#
# Each lane states only a `reservation_ratio`. The shared `llm_lane_budget` block and the Worker's
# daily row cap (`DO_ROWS_ENQUEUE_STOP` in config/dispatch_tuning.yml) decide the totals. Live lanes
# (`live: true`) first split a fixed `live_reservation_jobs` pool by their ratios; its rows come
# off the top, and the other lanes split the rest of the cap's rows in their ratios. Adding or
# removing a lane re-splits the budget with no other edit.
# ---------------------------------------------------------------------------------------------

ROW_CAP_TUNING_NAME = "DO_ROWS_ENQUEUE_STOP"
_BUDGET_FIELDS = frozenset(
    {
        "rows_per_write_unit",
        "execution_rows_per_job",
        "fixed_rows_per_day",
        "queue_days",
        "live_reservation_jobs",
    }
)
_CAP_FIELDS = ("reserved_write_units", "daily_write_units", "max_queued_jobs")


@dataclass(frozen=True)
class LaneBudget:
    """The shared inputs that split the daily row cap into lane reservations.

    ``row_cap`` is the Worker's daily row stop (``DO_ROWS_ENQUEUE_STOP``). One admitted job of a
    lane costs ``rows_per_write_unit`` x its ingress units at enqueue plus
    ``execution_rows_per_job`` to claim, complete, retire and clean up; ``fixed_rows_per_day``
    covers recounts and deploy schema retries. ``live_reservation_jobs`` is the fixed pool the
    live lanes share, taken off the row budget before the other lanes split it.
    """

    row_cap: int
    rows_per_write_unit: float
    execution_rows_per_job: float
    fixed_rows_per_day: int
    queue_days: int
    live_reservation_jobs: int = 0

    def rows_per_job(self, units_per_job: int) -> float:
        """Estimated billed DO rows per admitted job (enqueue rows plus execution rows)."""
        return self.rows_per_write_unit * units_per_job + self.execution_rows_per_job


def parse_lane_budget(raw: Any, *, row_cap: int) -> LaneBudget | None:
    """Validate the ``llm_lane_budget`` block; ``None`` when the site config has none."""
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError("llm_lane_budget must be a mapping")
    keys = set(raw)
    if keys != _BUDGET_FIELDS:
        raise ValueError(
            f"llm_lane_budget must define exactly {sorted(_BUDGET_FIELDS)}; "
            f"missing {sorted(_BUDGET_FIELDS - keys)}, unknown {sorted(keys - _BUDGET_FIELDS)}"
        )
    cap = _coerce_int(row_cap, purpose="llm_lane_budget", field=ROW_CAP_TUNING_NAME)
    fixed = _coerce_int(
        raw["fixed_rows_per_day"], purpose="llm_lane_budget", field="fixed_rows_per_day"
    )
    if cap <= fixed:
        raise ValueError(
            f"{ROW_CAP_TUNING_NAME} ({cap}) leaves no rows after fixed_rows_per_day ({fixed})"
        )
    queue_days = _coerce_int(raw["queue_days"], purpose="llm_lane_budget", field="queue_days")
    if queue_days == 0:
        raise ValueError("llm_lane_budget.queue_days must be positive")
    return LaneBudget(
        row_cap=cap,
        rows_per_write_unit=_coerce_number(
            raw["rows_per_write_unit"], where="llm_lane_budget.rows_per_write_unit"
        ),
        execution_rows_per_job=_coerce_number(
            raw["execution_rows_per_job"], where="llm_lane_budget.execution_rows_per_job"
        ),
        fixed_rows_per_day=fixed,
        queue_days=queue_days,
        live_reservation_jobs=_coerce_int(
            raw["live_reservation_jobs"], purpose="llm_lane_budget", field="live_reservation_jobs"
        ),
    )


def read_row_cap(tuning_path: str | Path) -> int:
    """Read ``DO_ROWS_ENQUEUE_STOP`` from ``dispatch_tuning.yml``; raise if it is not declared."""
    import yaml

    raw = yaml.safe_load(Path(tuning_path).read_text(encoding="utf-8")) or {}
    value = raw.get(ROW_CAP_TUNING_NAME) if isinstance(raw, Mapping) else None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(
            f"{tuning_path} does not declare a positive integer {ROW_CAP_TUNING_NAME}; lane "
            "reservations are split from that row cap and cannot be computed without it"
        )
    return value


def _floor_jobs(value: float) -> int:
    # Rounded to 9 places first so float noise (e.g. 344.9999999998) does not drop a whole job.
    return math.floor(round(value, 9))


def live_reservation_jobs(lanes: Mapping[str, LaneConfig], budget: LaneBudget) -> dict[str, int]:
    """Split ``live_reservation_jobs`` among the live lanes by their ratios (floored)."""
    live = {p: lane for p, lane in lanes.items() if lane.live}
    if not live:
        return {}
    weight = sum(lane.reservation_ratio for lane in live.values())
    if weight <= 0:
        raise ValueError(
            "llm_lanes: every live lane's reservation_ratio is 0, so the live_reservation_jobs "
            "pool has nothing to split"
        )
    pool = budget.live_reservation_jobs
    return {p: _floor_jobs(pool * lane.reservation_ratio / weight) for p, lane in live.items()}


def live_reservation_rows(lanes: Mapping[str, LaneConfig], budget: LaneBudget) -> float:
    """The live pool's rows, taken off the row budget before the other lanes split it."""
    return sum(
        jobs * budget.rows_per_job(lanes[p].ingress_write_units_per_job)
        for p, jobs in live_reservation_jobs(lanes, budget).items()
    )


def jobs_per_ratio_point(lanes: Mapping[str, LaneConfig], budget: LaneBudget) -> float:
    """``k`` such that the non-live lanes' reservations cost exactly the rows left to split.

    sum over non-live lanes of ratio x k x rows_per_job
        = row_cap - fixed_rows_per_day - the live pool's rows.
    """
    rows = budget.row_cap - budget.fixed_rows_per_day - live_reservation_rows(lanes, budget)
    if rows <= 0:
        raise ValueError(
            f"llm_lane_budget.live_reservation_jobs ({budget.live_reservation_jobs}) leaves no "
            f"rows of {ROW_CAP_TUNING_NAME} ({budget.row_cap}) for the other lanes"
        )
    weight = sum(
        lane.reservation_ratio * budget.rows_per_job(lane.ingress_write_units_per_job)
        for lane in lanes.values()
        if not lane.live
    )
    if weight <= 0:
        raise ValueError(
            "llm_lanes: every non-live reservation_ratio is 0, so there is nothing to split"
        )
    return rows / weight


def allocate_reservations(
    lanes: Mapping[str, LaneConfig], budget: LaneBudget
) -> dict[str, LaneConfig]:
    """Fill in each lane's three caps from its ratio and the shared row budget.

    - live lanes: reservation jobs = their ratio's share of ``live_reservation_jobs``;
    - other lanes: reservation jobs = floor(ratio x k) (see :func:`jobs_per_ratio_point`), so the
      total never passes the cap; reserved_write_units = jobs x the lane's ingress units per job;
    - daily_write_units = the sum of every lane's reservation, so a lane may borrow any other
      lane's unspent share as the Worker's ``_reservationBorrowFraction`` schedule releases it;
    - max_queued_jobs = queue_days x the lane's reservation jobs, or x the largest lane's for a
      lane that reserves nothing and runs only on borrowed capacity.
    """
    live_jobs = live_reservation_jobs(lanes, budget)
    jobs = dict(live_jobs)
    if any(not lane.live for lane in lanes.values()):
        k = jobs_per_ratio_point(lanes, budget)
        jobs.update(
            {
                p: _floor_jobs(lane.reservation_ratio * k)
                for p, lane in lanes.items()
                if not lane.live
            }
        )
    reserved = {p: jobs[p] * lanes[p].ingress_write_units_per_job for p in lanes}
    total_units = sum(reserved.values())
    largest = max(jobs.values())
    return {
        purpose: replace(
            lane,
            reserved_write_units=reserved[purpose],
            daily_write_units=total_units,
            max_queued_jobs=budget.queue_days * (jobs[purpose] or largest),
        )
        for purpose, lane in lanes.items()
    }


REASONING_LEVELS = frozenset({"off", "low"})


def _parse_reasoning(raw: Any, purpose: str, lane_models: set[str]) -> tuple[tuple[str, str], ...]:
    """Validate a lane's ``reasoning`` map: only this lane's models, only known levels."""
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise ValueError(f"llm_lanes[{purpose!r}].reasoning must be a mapping of model -> level")
    pairs: list[tuple[str, str]] = []
    for model, level in raw.items():
        if model not in lane_models:
            raise ValueError(
                f"llm_lanes[{purpose!r}].reasoning names {model!r}, which is not one of the lane's "
                "models or backup_models"
            )
        if level not in REASONING_LEVELS:
            raise ValueError(
                f"llm_lanes[{purpose!r}].reasoning[{model!r}] must be one of "
                f'{sorted(REASONING_LEVELS)}, got {level!r} (quote "off": bare off is YAML false)'
            )
        pairs.append((model, level))
    return tuple(sorted(pairs))


def _parse_model_list(raw: Any, purpose: str, field: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise ValueError(f"llm_lanes[{purpose!r}].{field} must be a list")
    models: tuple[str, ...] = ()
    for index, model in enumerate(raw):
        if not isinstance(model, str) or not model.strip():
            raise ValueError(
                f"llm_lanes[{purpose!r}].{field}[{index}] must be a non-empty string, got {model!r}"
            )
        models += (model.strip(),)
    if len(set(models)) != len(models):
        raise ValueError(f"llm_lanes[{purpose!r}].{field} contains duplicates: {models}")
    return models


def _parse_slots(
    raw: Any, purpose: str, models: tuple[str, ...], eligible: tuple[str, ...]
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Each active/eligible model sits in exactly one slot; each slot has an active model."""
    if raw is None:
        return ()
    if not isinstance(raw, Mapping) or not raw:
        raise ValueError(f"llm_lanes[{purpose!r}].slots must be a non-empty mapping")
    seen: dict[str, str] = {}
    pairs: list[tuple[str, tuple[str, ...]]] = []
    for name, raw_members in raw.items():
        members = _parse_model_list(raw_members, purpose, f"slots.{name}")
        if not members:
            raise ValueError(f"llm_lanes[{purpose!r}].slots.{name} must list at least one model")
        for model in members:
            if model not in models and model not in eligible:
                raise ValueError(
                    f"llm_lanes[{purpose!r}].slots.{name} names {model!r}, which is neither an "
                    "active nor an eligible model of the lane"
                )
            if model in seen:
                raise ValueError(
                    f"llm_lanes[{purpose!r}]: {model!r} is in slots {seen[model]!r} and {name!r}"
                )
            seen[model] = str(name)
        if not set(members) & set(models):
            raise ValueError(f"llm_lanes[{purpose!r}].slots.{name} has no active model")
        pairs.append((str(name), members))
    missing = [model for model in (*models, *eligible) if model not in seen]
    if missing:
        raise ValueError(f"llm_lanes[{purpose!r}]: models without a slot: {missing}")
    return tuple(sorted(pairs))


def parse_families(raw: Any, lanes: Mapping[str, LaneConfig]) -> dict[str, str]:
    """Validate ``llm_families`` (model -> family) against every lane model.

    Judge independence (review/49 section 4c, review/53) is decided per entry by family, so a lane
    model with no family would silently compare as independent of everything. Every active,
    backup and eligible model of every lane must therefore have one.
    """
    if not isinstance(raw, Mapping) or not raw:
        raise ValueError("site config must define a non-empty 'llm_families' mapping")
    families: dict[str, str] = {}
    for model, family in raw.items():
        if not isinstance(model, str) or not model.strip():
            raise ValueError(f"llm_families has an invalid model key {model!r}")
        if not isinstance(family, str) or not family.strip():
            raise ValueError(f"llm_families[{model!r}] must be a non-empty family name")
        families[model.strip()] = family.strip()
    missing = sorted(
        {
            model
            for lane in lanes.values()
            for model in (*lane.models, *lane.backup_models, *lane.eligible_models)
        }
        - set(families)
    )
    if missing:
        raise ValueError(f"llm_families has no family for lane model(s): {missing}")
    return families


def parse_lanes(raw_block: Any, *, budget: LaneBudget | None = None) -> dict[str, LaneConfig]:
    """Validate and normalize a raw ``llm_lanes`` mapping.

    Split from :func:`load_lanes` so the compiler and the tests can validate a block without
    touching the filesystem or the module-level cache. Lanes either all state a
    ``reservation_ratio`` (and ``budget`` splits the row cap between them) or all state their caps;
    :func:`parse_site_lanes` resolves ``budget`` from a whole site config.
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
        # Coercing with `str()` would have turned a YAML `null`, a bare number, or an empty
        # string into the routes "None", "3", and "" -- names no entry in llm_routes.json can
        # match. Such a lane compiles and admits jobs at ingress, and every one of them is then
        # indexed under the `__unroutable__` sentinel and never claimed: quota spent on work that
        # can never run. Reject the value where it is written instead.
        models: tuple[str, ...] = ()
        for index, model in enumerate(raw_models):
            if not isinstance(model, str) or not model.strip():
                raise ValueError(
                    f"llm_lanes[{purpose!r}].models[{index}] must be a non-empty string, "
                    f"got {model!r}"
                )
            models += (model.strip(),)
        if len(set(models)) != len(models):
            raise ValueError(f"llm_lanes[{purpose!r}].models contains duplicates: {models}")

        shape = entry.get("dispatch_shape", "pooled")
        if shape not in {"pooled", "per_model"}:
            raise ValueError(
                f"llm_lanes[{purpose!r}].dispatch_shape must be 'pooled' or 'per_model', "
                f"got {shape!r}"
            )
        max_queued_jobs = (
            _coerce_int(entry.get("max_queued_jobs"), purpose=purpose, field="max_queued_jobs")
            if entry.get("max_queued_jobs") is not None
            else None
        )
        reservation_ratio = None
        if "reservation_ratio" in entry:
            # review/58 section 12.11: the caps are derived, so stating them too would leave two
            # sources for one number.
            stated = [field for field in _CAP_FIELDS if field in entry]
            if stated:
                raise ValueError(
                    f"llm_lanes[{purpose!r}] has a reservation_ratio and also states {stated}; "
                    "its caps are computed from the ratio, so remove the stated values"
                )
            if budget is None:
                raise ValueError(
                    f"llm_lanes[{purpose!r}] has a reservation_ratio but the site config has no "
                    "llm_lane_budget block to split the row cap with"
                )
            reservation_ratio = _coerce_number(
                entry["reservation_ratio"], where=f"llm_lanes[{purpose!r}].reservation_ratio"
            )
            # Placeholders until every lane is known; allocate_reservations fills them in.
            reserved, daily = 0, 0
        elif "live" in entry:
            raise ValueError(
                f"llm_lanes[{purpose!r}] sets live but no reservation_ratio; a live lane's share "
                "of llm_lane_budget.live_reservation_jobs is set by its ratio"
            )
        else:
            reserved = _coerce_int(
                entry.get("reserved_write_units", 0),
                purpose=purpose,
                field="reserved_write_units",
            )
            daily = _coerce_int(
                entry.get("daily_write_units"), purpose=purpose, field="daily_write_units"
            )

        raw_backup_models = entry.get("backup_models")
        backup_after_attempts = entry.get("backup_after_attempts")
        has_backup_models = raw_backup_models is not None
        has_threshold = backup_after_attempts is not None
        if not has_backup_models and not has_threshold:
            backup_models: tuple[str, ...] = ()
        else:
            if has_backup_models != has_threshold or (
                not isinstance(raw_backup_models, (list, tuple)) or not raw_backup_models
            ):
                raise ValueError(
                    f"llm_lanes[{purpose!r}].backup_models must be a non-empty list when "
                    "backup_after_attempts is set (and vice versa)"
                )
            backup_models = ()
            for index, model in enumerate(raw_backup_models):
                if not isinstance(model, str) or not model.strip():
                    raise ValueError(
                        f"llm_lanes[{purpose!r}].backup_models[{index}] must be a non-empty "
                        f"string, got {model!r}"
                    )
                backup_models += (model.strip(),)
            if len(set(backup_models)) != len(backup_models):
                raise ValueError(
                    f"llm_lanes[{purpose!r}].backup_models contains duplicates: {backup_models}"
                )
            # A backup may also be one of the lane's own `models`. That adds no routes -- the
            # Worker de-duplicates them (routes.js routesEligibleFor) -- but a non-empty
            # backup_models is what raises the Worker's per-class retry ceilings to
            # backup_after_attempts + base (coordinator.js `_retryCeiling`), so a pooled lane can
            # keep that retry budget without dedicating a weaker model as a fallback.
            if shape == "per_model":
                raise ValueError(
                    f"llm_lanes[{purpose!r}] is dispatch_shape 'per_model' (one pinned route "
                    "per request) and cannot also declare backup_models (a failure-based "
                    "fallback)"
                )
            backup_after_attempts = _coerce_int(
                backup_after_attempts, purpose=purpose, field="backup_after_attempts"
            )
            if backup_after_attempts <= 0:
                raise ValueError(
                    f"llm_lanes[{purpose!r}].backup_after_attempts must be positive, got "
                    f"{backup_after_attempts}"
                )

        telemetry = entry.get("telemetry")
        if not isinstance(telemetry, Mapping) or set(telemetry) != {
            "producer",
            "unit",
            "completion",
            "scope",
        }:
            raise ValueError(f"llm_lanes[{purpose!r}] requires a telemetry contract")
        producer = telemetry["producer"]
        if not isinstance(producer, str) or not producer.strip():
            raise ValueError(f"llm_lanes[{purpose!r}].telemetry.producer must be non-empty")
        producer = producer.strip()
        if not isinstance(telemetry["unit"], str) or not telemetry["unit"].strip():
            raise ValueError(f"llm_lanes[{purpose!r}].telemetry.unit is invalid")
        unit = telemetry["unit"].strip()
        if not isinstance(telemetry["scope"], str) or telemetry["scope"] not in {
            "retained_catalog",
            "sample",
        }:
            raise ValueError(f"llm_lanes[{purpose!r}].telemetry.scope is invalid")
        if telemetry["completion"] != "consumed":
            raise ValueError(f"llm_lanes[{purpose!r}].telemetry.completion must be consumed")

        eligible_models = _parse_model_list(
            entry.get("eligible_models"), purpose, "eligible_models"
        )
        overlap = set(eligible_models) & {*models, *backup_models}
        if overlap:
            raise ValueError(
                f"llm_lanes[{purpose!r}].eligible_models must not repeat active or backup models: "
                f"{sorted(overlap)}"
            )
        slots = _parse_slots(entry.get("slots"), purpose, models, eligible_models)
        reasoning = _parse_reasoning(entry.get("reasoning"), purpose, {*models, *backup_models})
        lane = LaneConfig(
            purpose=purpose,
            models=models,
            telemetry_producer=producer,
            telemetry_unit=unit,
            telemetry_completion=telemetry["completion"],
            telemetry_scope=telemetry["scope"],
            max_dispatches_per_run=(
                _coerce_int(
                    entry.get("max_dispatches_per_run"),
                    purpose=purpose,
                    field="max_dispatches_per_run",
                )
                if entry.get("max_dispatches_per_run") is not None
                else None
            ),
            max_queued_jobs=max_queued_jobs,
            reserved_write_units=reserved,
            daily_write_units=daily,
            reservation_ratio=reservation_ratio,
            live=_coerce_bool(entry.get("live", False), purpose=purpose, field="live"),
            dispatch_shape=shape,
            backup_models=backup_models,
            backup_after_attempts=backup_after_attempts,
            catalog_backup_candidates=_coerce_bool(
                entry.get("catalog_backup_candidates", True),
                purpose=purpose,
                field="catalog_backup_candidates",
            ),
            reasoning=reasoning,
            eligible_models=eligible_models,
            slots=slots,
        )
        lanes[purpose] = lane

    ratioed = {p for p, lane in lanes.items() if lane.reservation_ratio is not None}
    if ratioed and ratioed != set(lanes):
        raise ValueError(
            "llm_lanes: either every lane states a reservation_ratio or none does, so the row "
            f"budget is split across all of them; missing on {sorted(set(lanes) - ratioed)}"
        )
    if ratioed:
        lanes = allocate_reservations(lanes, budget)
    for lane in lanes.values():
        _check_lane_caps(lane)
    return lanes


def _check_lane_caps(lane: LaneConfig) -> None:
    purpose, daily = lane.purpose, lane.daily_write_units
    if lane.reserved_write_units > daily:
        raise ValueError(
            f"llm_lanes[{purpose!r}] reserves {lane.reserved_write_units} write units but caps its "
            f"day at {daily}; a reservation it can never spend strands capacity from every other "
            "lane"
        )
    if daily < lane.ingress_write_units_per_job:
        raise ValueError(
            f"llm_lanes[{purpose!r}].daily_write_units ({daily}) is below the "
            f"{lane.ingress_write_units_per_job} units one of its jobs costs, so the lane can "
            "never admit a single job"
        )
    if (
        lane.max_dispatches_per_run is not None
        and lane.max_dispatches_per_run > lane.max_jobs_per_day
    ):
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


def tuning_path_for(site_config_path: str | Path) -> Path:
    """``dispatch_tuning.yml`` beside a site config, else the repository's own copy."""
    beside = Path(site_config_path).parent / "dispatch_tuning.yml"
    return beside if beside.exists() else _REPO_ROOT / "config" / "dispatch_tuning.yml"


def resolve_lane_budget(
    raw_site: Mapping[str, Any], *, tuning_path: str | Path
) -> LaneBudget | None:
    """The site config's ``llm_lane_budget`` with the row cap read from ``tuning_path``."""
    raw = raw_site.get("llm_lane_budget")
    if raw is None:
        return None
    return parse_lane_budget(raw, row_cap=read_row_cap(tuning_path))


def parse_site_lanes(
    raw_site: Mapping[str, Any], *, tuning_path: str | Path
) -> dict[str, LaneConfig]:
    """Parse ``llm_lanes`` from a whole site config, splitting reservations by ratio."""
    return parse_lanes(
        raw_site.get("llm_lanes"), budget=resolve_lane_budget(raw_site, tuning_path=tuning_path)
    )


_CACHE: dict[str, dict[str, LaneConfig]] = {}
_FAMILIES_CACHE: dict[str, dict[str, str]] = {}
_CACHE_LOCK = threading.Lock()


def load_lanes(*, path: str | Path = DEFAULT_SITE_CONFIG_PATH) -> dict[str, LaneConfig]:
    """Return the registry compiled from the ``llm_lanes`` block in ``path``.

    **The registry is repository-level policy, not per-deployment site content.** It is read from
    one file and never from a caller-supplied ``site_config`` mapping, for two reasons:

    * The Worker half cannot honor an override. ``scripts/compile_llm_lanes.py`` builds
      ``ingress_reservations.json`` from this same committed file, and the deploy workflow fails
      on drift. A run that resolved its models or budgets from some *other* config would submit
      jobs the deployed Worker has never heard of (rejected ``purpose_not_registered``) or charge
      them against budgets that config does not describe -- the override would look like it
      worked in the producer and quietly not exist at ingress.
    * Several lanes resolve at import time to build recipe hashes (agenda chapters, the locator,
      the tagger). A per-run model choice would make the same artifact hash differently between
      two runs of the same catalog and re-queue it, which is the backfill story AGENTS.md
      requires be deliberate.

    So a config selected with ``--site-config`` controls site content -- ``custom_domain``,
    ``tagging.enabled`` -- and never
    which purposes exist or what each may spend. ``path`` stays parameterized for the compiler and
    for tests; nothing in the pipeline passes it.

    Results are cached per resolved path because every per-episode stage call asks for its lane.
    """
    key = str(Path(path))
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached is not None:
            return cached

    import yaml

    raw = yaml.safe_load(Path(key).read_text()) or {}
    lanes = parse_site_lanes(raw, tuning_path=tuning_path_for(key))
    with _CACHE_LOCK:
        _CACHE[key] = lanes
    return lanes


def lane_for(purpose: str, *, path: str | Path = DEFAULT_SITE_CONFIG_PATH) -> LaneConfig:
    """Return the lane for ``purpose``, raising :class:`UnregisteredLaneError` if there is none.

    This is the only supported way for a call site to learn its models. Callers must not fall back
    to a hard-coded route when this raises: a lane the Worker will reject at ingress
    (``purpose_not_registered``) should fail here, in the producer, where the error names the
    config file to edit.

    There is deliberately no per-run override -- see :func:`load_lanes` for why the registry is
    repository-level.
    """
    lanes = load_lanes(path=path)
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


def load_families(*, path: str | Path = DEFAULT_SITE_CONFIG_PATH) -> dict[str, str]:
    """The validated ``llm_families`` map from ``path`` (model -> family).

    Cached per resolved path like :func:`load_lanes`: the judge's dirtiness check asks for it once
    per candidate episode, and re-parsing the 60 KB site config each time (pure-Python YAML) cost
    about 12 minutes of every judge run before it did any work (25,283 episodes, 2026-10-17).
    Callers get their own copy."""
    key = str(Path(path))
    with _CACHE_LOCK:
        cached = _FAMILIES_CACHE.get(key)
        if cached is not None:
            return dict(cached)

    import yaml

    raw = yaml.safe_load(Path(key).read_text(encoding="utf-8")) or {}
    families = parse_families(raw.get("llm_families"), load_lanes(path=key))
    with _CACHE_LOCK:
        _FAMILIES_CACHE[key] = families
    return dict(families)


def clear_cache() -> None:
    """Drop the parsed-config cache (tests that write temporary site configs)."""
    with _CACHE_LOCK:
        _CACHE.clear()
        _FAMILIES_CACHE.clear()


__all__ = [
    "DEFAULT_SITE_CONFIG_PATH",
    "NON_DISPATCHING_PURPOSES",
    "LaneConfig",
    "LaneBudget",
    "UnregisteredLaneError",
    "allocate_reservations",
    "clear_cache",
    "jobs_per_ratio_point",
    "live_reservation_jobs",
    "live_reservation_rows",
    "lane_for",
    "load_lanes",
    "parse_lane_budget",
    "parse_lanes",
    "parse_site_lanes",
    "read_row_cap",
    "resolve_lane_budget",
    "tuning_path_for",
]
