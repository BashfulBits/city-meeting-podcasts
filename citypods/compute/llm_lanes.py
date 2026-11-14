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
    max_dispatches_per_run: int | None = None
    reserved_write_units: int = 0
    daily_write_units: int = 0
    max_queued_jobs: int | None = None
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
# Lane caps derived from meetings per day (review/58 section 12.11).
#
# A lane with a ``sizing`` entry does not state its three caps; they are computed from its demand
# per meeting and ONE shared input, new meetings per day, so adding cities raises them without a
# hand edit. The shared block is ``llm_lane_sizing`` in the same site config.
# ---------------------------------------------------------------------------------------------

LANE_SIZING_FIELDS = frozenset({"jobs_per_meeting", "headroom", "backlog_jobs", "drain_days"})
_SHARED_SIZING_FIELDS = frozenset(
    {
        "meetings_per_day_p90",
        "basis_active_feeds",
        "backlog_floor_jobs",
        "queue_days",
        "round_jobs_to",
        "rows_per_write_unit",
        "execution_rows_per_job",
        "fixed_rows_per_day",
    }
)
_CAP_FIELDS = ("reserved_write_units", "daily_write_units", "max_queued_jobs")


@dataclass(frozen=True)
class LaneSizing:
    """The shared inputs that turn each sized lane's per-meeting demand into caps.

    ``meetings_per_day_p90`` is the measured p90 of daily new meetings when the catalog had
    ``basis_active_feeds`` active feed configs. ``active_feeds`` is the count in the committed
    ``config/feeds`` today. The effective figure scales the measured p90 by the growth in active
    feeds and never falls below it, so the configured number is a floor.
    """

    meetings_per_day_p90: float
    basis_active_feeds: int
    backlog_floor_jobs: int
    queue_days: int
    round_jobs_to: int
    rows_per_write_unit: float
    execution_rows_per_job: float
    fixed_rows_per_day: int
    active_feeds: int = 0

    @property
    def meetings_per_day(self) -> float:
        """Effective new meetings per day: the measured p90, scaled up with active feeds."""
        if self.active_feeds <= self.basis_active_feeds:
            return self.meetings_per_day_p90
        return self.meetings_per_day_p90 * self.active_feeds / self.basis_active_feeds

    def rows_per_job(self, units_per_job: int) -> float:
        """Estimated billed DO rows per admitted job (enqueue rows plus execution rows)."""
        return self.rows_per_write_unit * units_per_job + self.execution_rows_per_job


def parse_lane_sizing(raw: Any, *, active_feeds: int = 0) -> LaneSizing | None:
    """Validate the ``llm_lane_sizing`` block; ``None`` when the site config has none."""
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError("llm_lane_sizing must be a mapping")
    keys = set(raw)
    if keys != _SHARED_SIZING_FIELDS:
        raise ValueError(
            "llm_lane_sizing must define exactly "
            f"{sorted(_SHARED_SIZING_FIELDS)}; missing {sorted(_SHARED_SIZING_FIELDS - keys)}, "
            f"unknown {sorted(keys - _SHARED_SIZING_FIELDS)}"
        )

    def whole(field: str, *, positive: bool) -> int:
        value = _coerce_int(raw[field], purpose="llm_lane_sizing", field=field)
        if positive and value == 0:
            raise ValueError(f"llm_lane_sizing.{field} must be positive")
        return value

    return LaneSizing(
        meetings_per_day_p90=_coerce_number(
            raw["meetings_per_day_p90"], where="llm_lane_sizing.meetings_per_day_p90", positive=True
        ),
        basis_active_feeds=whole("basis_active_feeds", positive=True),
        backlog_floor_jobs=whole("backlog_floor_jobs", positive=False),
        queue_days=whole("queue_days", positive=True),
        round_jobs_to=whole("round_jobs_to", positive=True),
        rows_per_write_unit=_coerce_number(
            raw["rows_per_write_unit"], where="llm_lane_sizing.rows_per_write_unit"
        ),
        execution_rows_per_job=_coerce_number(
            raw["execution_rows_per_job"], where="llm_lane_sizing.execution_rows_per_job"
        ),
        fixed_rows_per_day=whole("fixed_rows_per_day", positive=False),
        active_feeds=active_feeds,
    )


def count_active_feeds(feeds_dir: str | Path) -> int:
    """Feed configs in ``feeds_dir`` whose ``lifecycle.status`` is ``active`` (the default).

    Paused, dormant and retired feeds publish no or few new meetings, so they do not count.
    Files whose name starts with ``_`` are templates. A missing directory counts zero, which
    leaves the effective meetings per day at its configured floor.
    """
    directory = Path(feeds_dir)
    if not directory.is_dir():
        return 0
    import yaml

    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    count = 0
    for path in sorted(directory.glob("*.yml")):
        if path.name.startswith("_"):
            continue
        feed = yaml.load(path.read_text(encoding="utf-8"), Loader=loader) or {}
        lifecycle = feed.get("lifecycle") if isinstance(feed, Mapping) else None
        status = lifecycle.get("status") if isinstance(lifecycle, Mapping) else None
        if status in (None, "active"):
            count += 1
    return count


def _round_up(value: float, step: int) -> int:
    # Rounded to 9 places first so float noise (e.g. 30.000000000004) does not add a whole step.
    return step * math.ceil(round(value / step, 9))


def sized_caps(
    entry: Mapping[str, Any], sizing: LaneSizing, *, units_per_job: int, purpose: str
) -> tuple[int, int, int]:
    """``(reserved_write_units, daily_write_units, max_queued_jobs)`` for one sized lane.

    reservation jobs = jobs_per_meeting x meetings/day x headroom, rounded up to round_jobs_to;
    ceiling jobs = reservation + max(backlog_floor_jobs, backlog_jobs / drain_days rounded up);
    queue = queue_days x ceiling jobs. Units are jobs x the lane's ingress units per job.
    """
    where = f"llm_lanes[{purpose!r}].sizing"
    if not isinstance(entry, Mapping) or set(entry) != LANE_SIZING_FIELDS:
        raise ValueError(f"{where} must define exactly {sorted(LANE_SIZING_FIELDS)}")
    jobs_per_meeting = _coerce_number(entry["jobs_per_meeting"], where=f"{where}.jobs_per_meeting")
    headroom = _coerce_number(entry["headroom"], where=f"{where}.headroom", positive=True)
    backlog_jobs = _coerce_number(entry["backlog_jobs"], where=f"{where}.backlog_jobs")
    drain_days = _coerce_number(entry["drain_days"], where=f"{where}.drain_days", positive=True)

    step = sizing.round_jobs_to
    reserved_jobs = _round_up(jobs_per_meeting * sizing.meetings_per_day * headroom, step)
    allowance = max(sizing.backlog_floor_jobs, _round_up(backlog_jobs / drain_days, step))
    ceiling_jobs = reserved_jobs + allowance
    return (
        reserved_jobs * units_per_job,
        ceiling_jobs * units_per_job,
        sizing.queue_days * ceiling_jobs,
    )


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


def parse_lanes(raw_block: Any, *, sizing: LaneSizing | None = None) -> dict[str, LaneConfig]:
    """Validate and normalize a raw ``llm_lanes`` mapping.

    Split from :func:`load_lanes` so the compiler and the tests can validate a block without
    touching the filesystem or the module-level cache. ``sizing`` is required only when a lane
    has a ``sizing`` entry; :func:`parse_site_lanes` resolves it from a whole site config.
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
        if "sizing" in entry:
            # review/58 section 12.11: the caps are derived, so stating them too would leave two
            # sources for one number.
            stated = [field for field in _CAP_FIELDS if field in entry]
            if stated:
                raise ValueError(
                    f"llm_lanes[{purpose!r}] has a sizing entry and also states {stated}; "
                    "a sized lane's caps are computed, so remove the stated values"
                )
            if sizing is None:
                raise ValueError(
                    f"llm_lanes[{purpose!r}] has a sizing entry but the site config has no "
                    "llm_lane_sizing block (meetings per day) to size it with"
                )
            reserved, daily, max_queued_jobs = sized_caps(
                entry["sizing"],
                sizing,
                units_per_job=3 + (1 if shape == "per_model" else len(models)),
                purpose=purpose,
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
        if reserved > daily:
            raise ValueError(
                f"llm_lanes[{purpose!r}] reserves {reserved} write units but caps its day at "
                f"{daily}; a reservation it can never spend strands capacity from every other lane"
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
        lanes[purpose] = lane
    return lanes


def feeds_dir_for(site_config_path: str | Path) -> Path:
    """The feed-config directory that sits beside a site config (``config/feeds``)."""
    return Path(site_config_path).parent / "feeds"


def resolve_lane_sizing(raw_site: Mapping[str, Any], *, feeds_dir: str | Path) -> LaneSizing | None:
    """The site config's ``llm_lane_sizing`` with today's active feed count filled in."""
    raw = raw_site.get("llm_lane_sizing")
    if raw is None:
        return None
    return parse_lane_sizing(raw, active_feeds=count_active_feeds(feeds_dir))


def parse_site_lanes(
    raw_site: Mapping[str, Any], *, feeds_dir: str | Path
) -> dict[str, LaneConfig]:
    """Parse ``llm_lanes`` from a whole site config, sizing lanes from ``llm_lane_sizing``.

    ``feeds_dir`` is the committed feed registry whose active count scales meetings per day.
    """
    return parse_lanes(
        raw_site.get("llm_lanes"), sizing=resolve_lane_sizing(raw_site, feeds_dir=feeds_dir)
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
    lanes = parse_site_lanes(raw, feeds_dir=feeds_dir_for(key))
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
    "LaneSizing",
    "UnregisteredLaneError",
    "clear_cache",
    "count_active_feeds",
    "feeds_dir_for",
    "lane_for",
    "load_lanes",
    "parse_lane_sizing",
    "parse_lanes",
    "parse_site_lanes",
    "resolve_lane_sizing",
    "sized_caps",
]
