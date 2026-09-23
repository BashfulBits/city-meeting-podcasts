"""Reconcile free LLM routes against authenticated provider catalogs.

The default is a read-only plan summary.  ``--apply`` permits a narrowly-targeted edit of
``config/provider_limits.yml``; ``--sync-issues`` maintains one rolling GitHub issue; and
``--open-pr`` pushes the resulting reviewed change to a content-addressed branch.  None of those
flags merges or deploys the change.

Availability is deliberately not treated as pricing or quality: a new route needs provider-specific
free evidence, an independent Artificial Analysis comparison against the configured quality floor
(or a strict Hugging Face official-evaluation fallback), and a successful minimal completion canary.
An existing route is removed only after it is absent from the catalog and its canary explicitly says
that the model is invalid or not found.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
import yaml

# `python scripts/reconcile_provider_routes.py` sets sys.path to scripts/, not the repository.
# Keep the shared rate-limit classifier import usable in both that supported invocation and tests.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

LIMITS_PATH = REPO_ROOT / "config" / "provider_limits.yml"
ISSUE_TITLE = "Provider catalog reconciliation — inconclusive free-route evidence"
ISSUE_MARKER = "<!-- citypods:provider-catalog-reconcile v=1 -->"
ISSUE_LABELS = (
    "type:operations",
    "area:provider",
    "signal:endpoint-contract",
    "needs:human-verification",
)
PROMPT = "Reply exactly: ok"
MODEL_NOT_FOUND = re.compile(
    r"invalid model|model[_ -]?not[_ -]?found|model\b[^.]{0,80}\bdoes not exist", re.I
)
ACCESS_RESTRICTED = re.compile(
    r"do(?:es)? not have access|not authori[sz]ed|permission denied|forbidden|not accessible",
    re.I,
)
PAYMENT_REQUIRED = re.compile(
    r"insufficient (balance|budget|credit)|no resource package|payment|required|subscription tier",
    re.I,
)

# Groq's configured account plan makes every model exposed by its authenticated catalog a free-route
# candidate. Airforce exposes a per-model marker; NVIDIA needs a second Build-card scrape.
# Mistral's free-mode account treats chat-capable catalog entries as quota-backed candidates.
# OpenRouter, Kilo, and OrcaRouter expose reviewed zero-price/free labels.
# Gemini is availability-only:
# Google documents that its Free Tier covers only certain models, and the list carries no tier flag.
FREE_CATALOG_PROVIDERS = {
    "airforce",
    "groq",
    "kilo",
    "mistral",
    "nvidia",
    "openrouter",
    "orcarouter",
}
NO_FREE_ROUTE_PROVIDERS = {"deepseek", "siliconflow"}
RECONCILED_PROVIDERS = {
    "airforce",
    "deepseek",
    "gemini",
    "groq",
    "kilo",
    "mistral",
    "nvidia",
    "openrouter",
    "orcarouter",
    "sambanova",
    "siliconflow",
    "zai",
}
# A provider can publish many candidates. The cap limits paid-by-quota completion probes only, never
# the preceding catalog/free/quality inspection that determines which candidates qualify for one.
MAX_NEW_CANARIES_PER_PROVIDER = 2
# NVIDIA often accepts a request slowly even when a route is usable.  A normal routing timeout is
# intentionally short, but the reconciliation canary is a one-off admission test, so grant that
# provider enough time to distinguish slow first-token service from an unavailable endpoint.
CANARY_TIMEOUT_SECONDS = {"nvidia": 90}
ARTIFICIAL_ANALYSIS_API_URL = "https://artificialanalysis.ai/api/v2/data/llms/models"
ARTIFICIAL_ANALYSIS_API_KEY_ENV = "ARTIFICIAL_ANALYSIS_API_KEY"
ARTIFICIAL_ANALYSIS_INTELLIGENCE_METRIC = "artificial_analysis_intelligence_index"
# The admission floor is the lower Intelligence Index of the two maintained general-text routes.
# Both records must be present; silently using just one would make a source omission change policy.
ARTIFICIAL_ANALYSIS_BASELINES = (
    ("openai", "gpt-oss-120b"),
    ("nvidia", "nvidia-nemotron-3-super-120b-a12b"),
)
ARTIFICIAL_ANALYSIS_BASELINE_LABEL = "GPT-OSS-120B / Nemotron-3 Super floor"
NVIDIA_BUILD_ORGANIZATION = "qc69jvmznzxy"
NVIDIA_BUILD_CATALOG_API = "https://api.ngc.nvidia.com/v2/search/catalog/resources/ENDPOINT"
GEMMA_QUALITY_BASELINE = "google/gemma-4-31b-it"
# The Hub fallback accepts only scores whose producer marked them verified. Its task values do not
# carry an explicit metric direction, so keep the allow-list narrow and direction-safe.
HUGGING_FACE_HIGHER_IS_BETTER_TASKS = {
    ("TIGER-Lab/MMLU-Pro", "mmlu_pro"),
    ("Idavidrein/gpqa", "diamond"),
    ("cais/hle", "hle"),
}
# Provider IDs occasionally use a commercial brand while the official Hub organization uses its
# publishing namespace. These are reviewed aliases, not fuzzy repository guesses.
HUGGING_FACE_ORGANIZATION_ALIASES = {"z-ai": "zai-org"}
# Provider namespace and model IDs are normalized mechanically, then compared for exact equality to
# Artificial Analysis's documented stable creator slug and model slug. These overrides cover cases
# where the two organizations intentionally use different publisher spelling; they are not fuzzy.
ARTIFICIAL_ANALYSIS_CREATOR_ALIASES = {
    "deepseek-ai": "deepseek",
    "meta-llama": "meta",
    "mistralai": "mistral",
    "moonshot": "kimi",
    "moonshotai": "kimi",
    "qwen": "alibaba",
    "z-ai": "zai",
    "zai": "zai",
}
# Some OpenAI-compatible catalogs use bare first-party IDs.  These publisher mappings are a
# prerequisite for comparing their identifiers to Artificial Analysis; they do not assert that a
# particular model is equivalent.
ARTIFICIAL_ANALYSIS_PROVIDER_CREATORS = {
    "deepseek": "deepseek",
    "gemini": "google",
    "mistral": "mistral",
    "zai": "zai",
}
# Reviewed identity aliases bridge an API's serving label to the exact Artificial Analysis record.
# Keep this deliberately small: an alias is model identity evidence, never a similarity heuristic.
ARTIFICIAL_ANALYSIS_MODEL_ALIASES = {
    ("google", "gemma-4-31b-it"): ("google", "gemma-4-31b"),
    ("nvidia", "nemotron-3-nano-omni-30b-a3b-reasoning"): (
        "nvidia",
        "nemotron-3-nano-omni-30b-a3b",
    ),
    ("nvidia", "nemotron-3-super-120b-a12b"): (
        "nvidia",
        "nvidia-nemotron-3-super-120b-a12b",
    ),
    ("nvidia", "nemotron-3-ultra-550b-a55b"): (
        "nvidia",
        "nvidia-nemotron-3-ultra-550b-a55b",
    ),
}


@dataclass(frozen=True)
class Probe:
    status: int | None
    classification: str
    summary: str


@dataclass
class Catalog:
    provider: str
    models: dict[str, dict[str, Any]] = field(default_factory=dict)
    error: str | None = None


@dataclass
class ArtificialAnalysisCatalog:
    """A run-scoped, independent model-evaluation catalog."""

    models: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


@dataclass
class NvidiaBuildCatalog:
    """Uncontracted public Build Catalog metadata, keyed by NVIDIA `/models` identifier."""

    free_by_model: dict[str, bool] = field(default_factory=dict)
    error: str | None = None


@dataclass(frozen=True)
class QualityEvidence:
    classification: str
    summary: str


@dataclass
class Plan:
    removals: set[str] = field(default_factory=set)
    additions: list[dict[str, Any]] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)
    observations: list[str] = field(default_factory=list)

    def changed(self) -> bool:
        return bool(self.removals or self.additions)

    def digest(self) -> str:
        payload = {
            "additions": [
                {key: value for key, value in route.items() if key != "_quality_comment"}
                for route in self.additions
            ],
            "removals": sorted(self.removals),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _http_evidence(status: int) -> str:
    """Return report-safe failure evidence without retaining a provider response body."""
    return f"HTTP {status}"


def _request_headers(provider: str, api_key: str) -> dict[str, str]:
    if provider == "gemini":
        return {"x-goog-api-key": api_key}
    return {"Authorization": f"Bearer {api_key}"}


def _catalog_url(provider: str, cfg: dict[str, Any]) -> str:
    if provider == "gemini":
        return "https://generativelanguage.googleapis.com/v1beta/models"
    if provider == "mistral":
        return f"{str(cfg['api_base']).rstrip('/')}/v1/models"
    return f"{str(cfg['api_base']).rstrip('/')}/models"


def _model_records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("data", "models"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _normalize_model_id(provider: str, model_id: str) -> str:
    return model_id.removeprefix("models/") if provider == "gemini" else model_id


def fetch_catalog(
    provider: str,
    cfg: dict[str, Any],
    *,
    get: Callable[..., requests.Response] = requests.get,
) -> Catalog:
    """Fetch a committed provider endpoint with an account key, never printing the response."""
    account = next(iter(cfg.get("accounts") or []), {})
    key_name = str(account.get("api_key_env") or "")
    api_key = os.environ.get(key_name)
    if not api_key:
        return Catalog(
            provider, error=f"missing credential {key_name or 'api key environment name'}"
        )
    try:
        response = get(
            _catalog_url(provider, cfg), headers=_request_headers(provider, api_key), timeout=20
        )
    except requests.RequestException as exc:
        return Catalog(provider, error=f"catalog request failed: {type(exc).__name__}")
    if not response.ok:
        return Catalog(provider, error=f"catalog {_http_evidence(response.status_code)}")
    try:
        payload = response.json()
    except ValueError:
        return Catalog(provider, error="catalog response was not JSON")
    models: dict[str, dict[str, Any]] = {}
    for item in _model_records(payload):
        raw_id = item.get("id") or item.get("name")
        if isinstance(raw_id, str) and raw_id:
            models[_normalize_model_id(provider, raw_id)] = item
    return Catalog(provider, models=models)


def _canary_url(provider: str, cfg: dict[str, Any]) -> str:
    # Airforce's api_base already ends in /v1, while its compiler chat_path retains the upstream
    # /v1 prefix for the AI Gateway representation.  The direct catalog canary must not double it.
    path = "/chat/completions" if provider == "airforce" else str(cfg.get("chat_path") or "")
    return f"{str(cfg['api_base']).rstrip('/')}/{path.lstrip('/')}"


def canary(
    provider: str,
    cfg: dict[str, Any],
    model: str,
    *,
    post: Callable[..., requests.Response] = requests.post,
) -> Probe:
    """Send one bounded, non-sensitive streaming completion and classify durable meaning only."""
    account = next(iter(cfg.get("accounts") or []), {})
    key_name = str(account.get("api_key_env") or "")
    api_key = os.environ.get(key_name)
    if not api_key:
        return Probe(
            None, "inconclusive", f"missing credential {key_name or 'api key environment name'}"
        )
    headers = _request_headers(provider, api_key) | {"Content-Type": "application/json"}
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": PROMPT}],
        "max_tokens": 4,
        "temperature": 0,
    }
    try:
        response = post(
            _canary_url(provider, cfg),
            headers=headers,
            json=payload,
            timeout=CANARY_TIMEOUT_SECONDS.get(provider, 30),
            stream=True,
        )
    except requests.RequestException as exc:
        return Probe(None, "inconclusive", f"canary failed: {type(exc).__name__}")
    if response.ok:
        # A streaming response returns once the provider has accepted and opened the completion;
        # do not hold an admission check open merely to receive four trivial tokens.  This keeps
        # slow but usable NVIDIA routes from being misreported as read timeouts.
        close = getattr(response, "close", None)
        if callable(close):
            close()
        return Probe(response.status_code, "success", f"completion HTTP {response.status_code}")
    response_text = response.text
    evidence = _http_evidence(response.status_code)
    if ACCESS_RESTRICTED.search(response_text) or PAYMENT_REQUIRED.search(response_text):
        return Probe(response.status_code, "entitlement", evidence)
    if response.status_code in {400, 404} and MODEL_NOT_FOUND.search(response_text):
        return Probe(response.status_code, "missing", evidence)
    if response.status_code == 410 and "end of life" in response_text.lower():
        return Probe(response.status_code, "missing", evidence)
    try:
        body = response.json()
    except ValueError:
        body = response.text
    # Use the same taxonomy as the bounded rate-limit probe and production dispatcher.  The
    # import is intentionally lazy: this script also supports direct execution from scripts/.
    from citypods.compute.llm_failure_class import classify_provider_failure

    failure = classify_provider_failure(
        status=response.status_code,
        body=body,
        headers=getattr(response, "headers", {}),
        route={"provider": provider},
    )
    if failure.failure_class == "payment_required":
        return Probe(
            response.status_code,
            "entitlement",
            f"{evidence} ({failure.rule_id})",
        )
    return Probe(response.status_code, "inconclusive", evidence)


def _airforce_free(record: dict[str, Any]) -> bool:
    # `access_tiers` is a broad capability list: paid records currently include "free" there
    # while their authoritative `tier` remains "paid" and the completion returns HTTP 402.  Only
    # the record's own tier is a per-model free-route assertion.
    return str(record.get("tier") or "").lower() == "free"


def _is_zero(value: Any) -> bool:
    try:
        return float(str(value)) == 0
    except (TypeError, ValueError):
        return False


def _zero_text_pricing(record: dict[str, Any]) -> bool:
    """Require explicit zero prices for both text directions, never absent pricing metadata."""
    pricing = record.get("pricing")
    if not isinstance(pricing, dict):
        return False
    return (
        "prompt" in pricing
        and "completion" in pricing
        and all(_is_zero(pricing[key]) for key in ("prompt", "completion"))
    )


def _orcarouter_free(record: dict[str, Any], model_id: str) -> bool:
    """OrcaRouter's catalog marks its no-cost routes with both suffix and display label."""
    pricing = record.get("pricing")
    return (
        model_id.endswith("-free")
        and "(free)" in str(record.get("name") or "").lower()
        and isinstance(pricing, dict)
        and "request" in pricing
        and _is_zero(pricing["request"])
    )


def _chat_capable(provider: str, record: dict[str, Any]) -> bool:
    """Exclude catalog entries that cannot be used through this reconciler's chat endpoint."""
    if provider != "mistral":
        return True
    capabilities = record.get("capabilities")
    return isinstance(capabilities, dict) and capabilities.get("completion_chat") is True


def fetch_nvidia_build_catalog(
    *,
    get: Callable[..., requests.Response] = requests.get,
) -> NvidiaBuildCatalog:
    """Scrape the Build Catalog's public NGC metadata once, without treating it as a contract."""
    query = quote(
        json.dumps(
            {
                "query": "*",
                "filters": [{"field": "orgName", "value": NVIDIA_BUILD_ORGANIZATION}],
                "page": 0,
                "pageSize": 200,
            },
            separators=(",", ":"),
        )
    )
    try:
        response = get(
            f"{NVIDIA_BUILD_CATALOG_API}?q={query}",
            headers={"Accept": "application/json"},
            timeout=20,
        )
    except requests.RequestException as exc:
        return NvidiaBuildCatalog(error=f"request failed: {type(exc).__name__}")
    if not response.ok:
        return NvidiaBuildCatalog(error=_http_evidence(response.status_code))
    try:
        payload = response.json()
    except ValueError:
        return NvidiaBuildCatalog(error="response was not JSON")
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        return NvidiaBuildCatalog(error="response has no catalog results")
    free_by_model: dict[str, bool] = {}
    for group in payload["results"]:
        if not isinstance(group, dict):
            continue
        for resource in group.get("resources") or []:
            if (
                not isinstance(resource, dict)
                or resource.get("orgName") != NVIDIA_BUILD_ORGANIZATION
            ):
                continue
            name = resource.get("name")
            labels = resource.get("labels") or []
            if not isinstance(name, str) or not isinstance(labels, list):
                continue
            publisher: str | None = None
            label_values: set[str] = set()
            for label in labels:
                if not isinstance(label, dict):
                    continue
                values = label.get("values") or []
                if not isinstance(values, list):
                    continue
                if (
                    label.get("key") == "publisher"
                    and len(values) == 1
                    and isinstance(values[0], str)
                ):
                    publisher = values[0]
                label_values.update(str(value) for value in values)
            if publisher:
                free_by_model[f"{publisher}/{name}"] = "Free Endpoint" in label_values
    return NvidiaBuildCatalog(free_by_model=free_by_model)


def _nvidia_catalog_slug(model_id: str) -> str | None:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", model_id):
        return None
    return _normalized_slug(model_id.split("/", 1)[1])


def _nvidia_context_limit(description: str) -> int | None:
    patterns = (
        r"\b([0-9][0-9,]*(?:\.\d+)?\s*[KkMm]?)\s*-token\s+context\b",
        r"\bcontext\s+(?:length|window)\s+(?:up\s+to|of)\s+"
        r"([0-9][0-9,]*(?:\.\d+)?\s*[KkMm]?)\s+tokens\b",
    )
    for pattern in patterns:
        match = re.search(pattern, description, flags=re.I)
        if match is None:
            continue
        value = match.group(1).replace(",", "").replace(" ", "")
        suffix = value[-1:].lower()
        try:
            number = float(value[:-1] if suffix in {"k", "m"} else value)
        except ValueError:
            continue
        multiplier = 1024 if suffix == "k" else 1024 * 1024 if suffix == "m" else 1
        limit = int(number * multiplier)
        if limit > 0:
            return limit
    return None


def nvidia_catalog_limits(
    model_id: str,
    *,
    get: Callable[..., requests.Response] = requests.get,
) -> dict[str, int] | None:
    """Read a qualified NVIDIA model's published context limit, never guessing a default."""
    slug = _nvidia_catalog_slug(model_id)
    if slug is None:
        return None
    try:
        response = get(
            f"https://api.ngc.nvidia.com/v2/endpoints/{NVIDIA_BUILD_ORGANIZATION}/{quote(slug)}",
            headers={"Accept": "application/json"},
            timeout=20,
        )
    except requests.RequestException:
        return None
    if not response.ok:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    artifact = payload.get("artifact") if isinstance(payload, dict) else None
    description = artifact.get("description") if isinstance(artifact, dict) else None
    if not isinstance(description, str):
        return None
    context_length = _nvidia_context_limit(description)
    return {"context_length": context_length} if context_length else None


def _nvidia_free(
    model_id: str,
    *,
    get: Callable[..., requests.Response] = requests.get,
) -> bool | None:
    """Read the matching NVIDIA Build Catalog result; None is an inconclusive scraper failure."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", model_id):
        return None
    catalog_name = re.sub(r"[^a-z0-9]+", "-", model_id.rsplit("/", 1)[-1].lower()).strip("-")
    catalog_url = f"https://build.nvidia.com/models?q={quote(catalog_name, safe='')}"
    response: requests.Response | None = None
    for attempt in range(3):
        try:
            response = get(
                catalog_url,
                headers={"User-Agent": "citypods-provider-catalog-reconciler/1.0"},
                timeout=20,
                allow_redirects=False,
            )
        except requests.RequestException:
            return None
        # Build Catalog occasionally starts a search with an empty 202 response before rendering
        # the server-side result. It is not evidence that the model is paid or absent.
        if response.status_code != 202:
            break
        if attempt < 2:
            time.sleep(1)
    if response is None or response.status_code == 202:
        return None
    if not response.ok:
        return None
    # Individual model URLs can render an application-level 404 behind HTTP 200. The searchable
    # Catalog page renders both the model result and its endpoint badge. An absent result is not
    # public evidence of a free endpoint; transport and HTTP failures above remain inconclusive.
    text = response.text.lower()
    result_name = re.escape(catalog_name)
    for result in re.findall(r"<li\b[\s\S]*?</li>", text):
        if 'data-testid="nv-card-root"' not in result:
            continue
        if re.search(rf"\b{result_name}\b", result):
            return "free endpoint" in result
    return False


def is_free_evidence(
    provider: str,
    model_id: str,
    record: dict[str, Any],
    *,
    get: Callable[..., requests.Response] = requests.get,
    nvidia_catalog: NvidiaBuildCatalog | None = None,
) -> bool | None:
    """Return true/false only for explicit evidence; None means the source is inconclusive."""
    if provider == "airforce":
        return _airforce_free(record)
    if provider == "groq":
        return True
    if provider in {"openrouter", "kilo"}:
        # Both providers document and publish the literal :free variant.  The zero text price is
        # required as a second, machine-readable guard against a stale or misleading suffix.
        return model_id.endswith(":free") and _zero_text_pricing(record)
    if provider == "orcarouter":
        return _orcarouter_free(record, model_id)
    if provider == "mistral":
        # Mistral's Free Mode is account/quota backed rather than model-priced.  It publishes no
        # per-model free marker, so the bounded completion probe below is the required proof that
        # this account can actually use a chat-capable entry without a paid entitlement.
        return _chat_capable(provider, record)
    if provider == "nvidia":
        if nvidia_catalog is not None:
            if nvidia_catalog.error:
                return None
            direct = nvidia_catalog.free_by_model.get(model_id)
            if direct is not None:
                return direct
            if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", model_id):
                publisher, name = model_id.split("/", 1)
                # `/models` preserves publishers' punctuation (for example `glm-5.3`), whereas
                # Build Catalog slugs encode it as hyphens. Publisher plus normalized slug is an
                # exact cross-surface identity, not a similarity or family match.
                return nvidia_catalog.free_by_model.get(f"{publisher}/{_normalized_slug(name)}")
            return None
        return _nvidia_free(model_id, get=get)
    return None


def _catalog_admission_model(
    provider: str,
    model_id: str,
    record: dict[str, Any],
    catalog: Catalog,
) -> tuple[str, dict[str, Any]]:
    """Select one provider-declared serving ID for aliases that bill as the same Mistral model."""
    if provider != "mistral":
        return model_id, record
    billing_model = record.get("billing_model_name")
    if not isinstance(billing_model, str) or not billing_model:
        return model_id, record
    normalized = _normalize_model_id(provider, billing_model)
    if normalized in catalog.models:
        return normalized, catalog.models[normalized]
    return model_id, record


def _hf_repository(model_id: str, record: dict[str, Any]) -> str | None:
    """Use only an explicit mapping or an already provider-qualified repository identifier."""
    explicit = record.get("huggingface_repo")
    if isinstance(explicit, str) and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", explicit):
        return explicit
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", model_id):
        organization, name = model_id.split("/", 1)
        return f"{HUGGING_FACE_ORGANIZATION_ALIASES.get(organization, organization)}/{name}"
    return None


def _huggingface_model(
    repository: str,
    *,
    get: Callable[..., requests.Response],
) -> dict[str, Any] | None:
    url = f"https://huggingface.co/api/models/{quote(repository, safe='/')}?expand=evalResults"
    try:
        response = get(url, timeout=20)
    except requests.RequestException:
        return None
    if not response.ok:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _normalized_slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _artificial_analysis_identities(
    model_id: str,
    record: dict[str, Any],
    *,
    provider: str | None = None,
) -> list[tuple[str, str]]:
    """Resolve exact identities from provider-declared IDs, aliases, and billing names only."""
    explicit = record.get("artificial_analysis_model")
    if isinstance(explicit, dict):
        creator = explicit.get("creator")
        slug = explicit.get("slug")
        if isinstance(creator, str) and isinstance(slug, str) and creator and slug:
            return [(creator, slug)]
    # Preserve priority: the serving ID and billing identity describe the current endpoint, while
    # aliases often include historical compatibility names.  Quality resolution uses the first
    # matching identity, so an old alias cannot make a current billing model ambiguous.
    serving_model_id = model_id
    # These gateways deliberately decorate the upstream identity to indicate their no-cost route.
    # The suffix is route pricing evidence, not part of the publisher's model name in Artificial
    # Analysis. Strip it only for the providers that document this exact convention.
    if provider in {"kilo", "openrouter"} and serving_model_id.endswith(":free"):
        serving_model_id = serving_model_id.removesuffix(":free")
    elif provider == "orcarouter" and serving_model_id.endswith("-free"):
        serving_model_id = serving_model_id.removesuffix("-free")
    names = [serving_model_id]
    namespaced_creator: str | None = None
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", serving_model_id):
        namespaced_creator, namespaced_name = serving_model_id.split("/", 1)
        names = [namespaced_name]
    for key in ("billing_model_name", "name"):
        value = record.get(key)
        if isinstance(value, str):
            names.append(value)
    aliases = record.get("aliases")
    if isinstance(aliases, list):
        names.extend(alias for alias in aliases if isinstance(alias, str))

    creators: list[str] = []
    if namespaced_creator is not None:
        creators.append(
            ARTIFICIAL_ANALYSIS_CREATOR_ALIASES.get(
                namespaced_creator.lower(), namespaced_creator.lower()
            )
        )
    owner = record.get("owned_by")
    if isinstance(owner, str) and owner:
        creators.append(ARTIFICIAL_ANALYSIS_CREATOR_ALIASES.get(owner.lower(), owner.lower()))
    if provider in ARTIFICIAL_ANALYSIS_PROVIDER_CREATORS:
        creators.append(ARTIFICIAL_ANALYSIS_PROVIDER_CREATORS[provider])

    identities: list[tuple[str, str]] = []
    for creator in creators:
        for name in names:
            identity = ARTIFICIAL_ANALYSIS_MODEL_ALIASES.get(
                (creator, _normalized_slug(name)), (creator, _normalized_slug(name))
            )
            if identity not in identities:
                identities.append(identity)
    return identities


def _artificial_analysis_identity(
    model_id: str,
    record: dict[str, Any],
    *,
    provider: str | None = None,
) -> tuple[str, str] | None:
    """Return the primary exact identity; quality resolution considers every declared alias."""
    identities = _artificial_analysis_identities(model_id, record, provider=provider)
    return identities[0] if identities else None


def fetch_artificial_analysis_catalog(
    *,
    get: Callable[..., requests.Response] = requests.get,
) -> ArtificialAnalysisCatalog:
    """Fetch the independent quality source exactly once for a reconciliation run."""
    api_key = os.environ.get(ARTIFICIAL_ANALYSIS_API_KEY_ENV)
    if not api_key:
        return ArtificialAnalysisCatalog(
            error=f"missing credential {ARTIFICIAL_ANALYSIS_API_KEY_ENV}"
        )
    try:
        response = get(
            ARTIFICIAL_ANALYSIS_API_URL,
            headers={"x-api-key": api_key},
            timeout=20,
        )
    except requests.RequestException as exc:
        return ArtificialAnalysisCatalog(error=f"request failed: {type(exc).__name__}")
    if not response.ok:
        return ArtificialAnalysisCatalog(error=_http_evidence(response.status_code))
    try:
        payload = response.json()
    except ValueError:
        return ArtificialAnalysisCatalog(error="response was not JSON")
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return ArtificialAnalysisCatalog(error="response has no model data")
    return ArtificialAnalysisCatalog(models=[item for item in data if isinstance(item, dict)])


def _artificial_analysis_model(
    catalog: ArtificialAnalysisCatalog,
    identity: tuple[str, str],
) -> dict[str, Any] | None:
    creator, slug = identity
    matches = []
    for model in catalog.models:
        model_creator = model.get("model_creator")
        if not isinstance(model_creator, dict):
            continue
        if str(model.get("slug") or "") == slug and str(model_creator.get("slug") or "") == creator:
            matches.append(model)
    if len(matches) == 1:
        return matches[0]
    if matches:
        return None
    # Providers commonly append a four- or six-digit release build to an otherwise identical,
    # versioned model ID (for example, "model-3-2508").  Require that explicit generation marker:
    # accepting "model-2603" as "model" would conflate distinct annual releases.  Arbitrary
    # substrings are never model identity evidence.
    suffix_matches = []
    for model in catalog.models:
        model_creator = model.get("model_creator")
        candidate_slug = str(model.get("slug") or "")
        if not isinstance(model_creator, dict) or str(model_creator.get("slug") or "") != creator:
            continue
        prefix = f"{candidate_slug}-"
        has_generation = any(re.fullmatch(r"\d{1,2}", token) for token in candidate_slug.split("-"))
        if (
            has_generation
            and slug.startswith(prefix)
            and re.fullmatch(r"\d{4,6}", slug[len(prefix) :])
        ):
            suffix_matches.append(model)
    return suffix_matches[0] if len(suffix_matches) == 1 else None


def _artificial_analysis_score(model: dict[str, Any]) -> float | None:
    evaluations = model.get("evaluations")
    if not isinstance(evaluations, dict):
        return None
    try:
        score = float(evaluations[ARTIFICIAL_ANALYSIS_INTELLIGENCE_METRIC])
    except (KeyError, TypeError, ValueError):
        return None
    return score if score >= 0 else None


def _artificial_analysis_comment(
    provider: str,
    model_id: str,
    record: dict[str, Any],
    catalog: ArtificialAnalysisCatalog | None,
) -> str | None:
    """Return a dated, non-policy snapshot suitable for a human-facing YAML comment."""
    if catalog is None or catalog.error:
        return None
    candidate = next(
        (
            match
            for identity in _artificial_analysis_identities(model_id, record, provider=provider)
            if (match := _artificial_analysis_model(catalog, identity)) is not None
        ),
        None,
    )
    score = _artificial_analysis_score(candidate) if candidate else None
    baseline = _artificial_analysis_baseline(catalog)
    if score is None or baseline is None:
        return None
    baseline_score, _ = baseline
    observed = datetime.now(UTC).date().isoformat()
    return (
        f"AA Intelligence Index {score:g} >= admission floor {baseline_score:g} "
        f"(informational snapshot {observed}; methodology/scores can change)"
    )


def _artificial_analysis_baseline(
    catalog: ArtificialAnalysisCatalog,
) -> tuple[float, str] | None:
    """Return the lower score across the two user-selected admission-floor models."""
    scores = [
        _artificial_analysis_score(_artificial_analysis_model(catalog, identity) or {})
        for identity in ARTIFICIAL_ANALYSIS_BASELINES
    ]
    if any(score is None for score in scores):
        return None
    return min(score for score in scores if score is not None), ARTIFICIAL_ANALYSIS_BASELINE_LABEL


def _huggingface_verified_scores(record: dict[str, Any]) -> dict[tuple[str, str], float]:
    """Use only verified, direction-safe official Hub evaluation records as a fallback."""
    scores: dict[tuple[str, str], float] = {}
    for result in record.get("evalResults") or []:
        if not isinstance(result, dict) or result.get("verified") is not True:
            continue
        data = result.get("data") or {}
        dataset = data.get("dataset") or {}
        key = (str(dataset.get("id") or ""), str(dataset.get("task_id") or ""))
        if key not in HUGGING_FACE_HIGHER_IS_BETTER_TASKS:
            continue
        try:
            scores[key] = float(data["value"])
        except (KeyError, TypeError, ValueError):
            continue
    return scores


def _huggingface_quality_evidence(
    model_id: str,
    record: dict[str, Any],
    *,
    get: Callable[..., requests.Response],
    cache: dict[str, dict[str, Any] | None],
) -> QualityEvidence:
    repository = _hf_repository(model_id, record)
    if repository is None:
        return QualityEvidence("inconclusive", "no exact Hugging Face repository mapping")
    if repository not in cache:
        cache[repository] = _huggingface_model(repository, get=get)
    candidate = cache[repository]
    if candidate is None:
        return QualityEvidence(
            "inconclusive", f"Hugging Face evaluation metadata unavailable for {repository}"
        )
    if GEMMA_QUALITY_BASELINE not in cache:
        cache[GEMMA_QUALITY_BASELINE] = _huggingface_model(GEMMA_QUALITY_BASELINE, get=get)
    baseline = cache[GEMMA_QUALITY_BASELINE]
    if baseline is None:
        return QualityEvidence("inconclusive", "Gemma-4 fallback evaluation metadata unavailable")
    candidate_scores = _huggingface_verified_scores(candidate)
    baseline_scores = _huggingface_verified_scores(baseline)
    common = sorted(set(candidate_scores) & set(baseline_scores))
    if not common:
        return QualityEvidence("inconclusive", "no shared verified Hugging Face benchmark")
    if all(candidate_scores[key] >= baseline_scores[key] for key in common):
        return QualityEvidence(
            "qualified", f"meets Gemma-4 on {len(common)} verified Hugging Face benchmark(s)"
        )
    return QualityEvidence("rejected", "below Gemma-4 on a verified Hugging Face benchmark")


def quality_evidence(
    _provider: str,
    model_id: str,
    record: dict[str, Any],
    *,
    get: Callable[..., requests.Response] = requests.get,
    artificial_analysis: ArtificialAnalysisCatalog | None = None,
    huggingface_cache: dict[str, dict[str, Any] | None] | None = None,
) -> QualityEvidence:
    """Qualify exact models by independent evidence, with a verified-Hub fallback only.

    Artificial Analysis evaluates models independently with one shared composite benchmark. The
    optional Hub fallback deliberately excludes publisher card claims and unverified results.
    """
    catalog = artificial_analysis or fetch_artificial_analysis_catalog(get=get)
    identities = _artificial_analysis_identities(model_id, record, provider=_provider)
    if catalog.error is None and identities:
        candidate = next(
            (
                match
                for identity in identities
                if (match := _artificial_analysis_model(catalog, identity)) is not None
            ),
            None,
        )
        baseline = _artificial_analysis_baseline(catalog)
        candidate_score = _artificial_analysis_score(candidate) if candidate else None
        if candidate_score is not None and baseline is not None:
            baseline_score, baseline_label = baseline
            if candidate_score >= baseline_score:
                return QualityEvidence(
                    "qualified",
                    "Artificial Analysis Intelligence Index "
                    f"{candidate_score:g} >= {baseline_label} {baseline_score:g}",
                )
            return QualityEvidence(
                "rejected",
                "Artificial Analysis Intelligence Index "
                f"{candidate_score:g} < {baseline_label} {baseline_score:g}",
            )
    fallback = _huggingface_quality_evidence(
        model_id,
        record,
        get=get,
        cache=huggingface_cache if huggingface_cache is not None else {},
    )
    if fallback.classification != "inconclusive":
        return fallback
    if catalog.error:
        source = f"Artificial Analysis unavailable ({catalog.error})"
    elif not identities:
        source = "no exact Artificial Analysis model identity"
    else:
        source = "Artificial Analysis has no comparable exact-model score"
    return QualityEvidence("inconclusive", f"{source}; {fallback.summary}")


def _context_limit(record: dict[str, Any], keys: Iterable[str]) -> int | None:
    for key in keys:
        value = record.get(key)
        if isinstance(value, int) and value > 0:
            return value
        if isinstance(value, str) and value.isdigit() and int(value) > 0:
            return int(value)
    return None


def build_route(
    provider: str,
    cfg: dict[str, Any],
    model_id: str,
    record: dict[str, Any],
    *,
    quality_comment: str | None = None,
) -> dict[str, Any] | None:
    """Build a deliberately conservative route only when the catalog gives explicit limits."""
    input_limit = _context_limit(record, ("context_length", "context_window", "max_context_length"))
    output_limit = _context_limit(
        record,
        ("max_output_tokens", "max_completion_tokens", "output_context_limit"),
    )
    if input_limit is None:
        return None
    output_limit = min(output_limit or 4096, input_limit)
    account = next(iter(cfg.get("accounts") or []), {})
    safe_name = re.sub(r"[^a-z0-9]+", "_", model_id.lower()).strip("_")
    route: dict[str, Any] = {
        "route_id": f"{provider}_{safe_name}_catalog_free",
        "model": f"{provider}/{model_id}",
        "provider": provider,
        "upstream_model": model_id,
        "input_context_limit": input_limit,
        "output_context_limit": output_limit,
        "account_id": str(account.get("id") or "primary"),
        "rpm": 1,
        "concurrency": 1,
        "free": True,
        "auto_discovered": True,
    }
    if provider == "nvidia":
        route.update({"rpm": 0.5, "tpm": 100000})
    if quality_comment:
        # Private to the reconciliation plan: apply_route_changes emits it as YAML commentary and
        # removes it before serializing so dispatch semantics cannot depend on a benchmark score.
        route["_quality_comment"] = quality_comment
    return route


def _canonical_model(route: dict[str, Any]) -> str:
    return str(route.get("model_key") or route.get("model") or "")


def _missing_fallbacks(raw: dict[str, Any], removed: set[str]) -> list[str]:
    routes = [route for route in raw.get("routes", []) if isinstance(route, dict)]
    survivors = [
        route for route in routes if route.get("route_id") not in removed and route.get("free")
    ]
    survivor_models = {_canonical_model(route) for route in survivors}
    routing = raw.get("model_routing") or {}
    findings: list[str] = []
    for model in sorted(
        {_canonical_model(route) for route in routes if route.get("route_id") in removed}
    ):
        if not model or model in survivor_models:
            continue
        targets = routing.get(model) if isinstance(routing, dict) else None
        if isinstance(targets, list) and any(str(target) in survivor_models for target in targets):
            findings.append(f"{model}: removed final route; reviewed model_routing target remains")
        else:
            findings.append(
                f"{model}: removed final free route; explicit replacement model is required"
            )
    return findings


def plan_reconciliation(
    raw: dict[str, Any],
    providers_requested: set[str],
    *,
    get: Callable[..., requests.Response] = requests.get,
    post: Callable[..., requests.Response] = requests.post,
    quality: Callable[..., QualityEvidence] = quality_evidence,
) -> Plan:
    plan = Plan()
    artificial_analysis = (
        fetch_artificial_analysis_catalog(get=get) if quality is quality_evidence else None
    )
    huggingface_cache: dict[str, dict[str, Any] | None] = {}
    providers = raw.get("providers") or {}
    routes = [route for route in raw.get("routes", []) if isinstance(route, dict)]
    route_ids = {str(route.get("route_id") or "") for route in routes}
    selected = sorted(providers_requested or RECONCILED_PROVIDERS)
    for provider in selected:
        cfg = providers.get(provider)
        if not isinstance(cfg, dict):
            plan.findings.append(f"{provider}: unknown provider")
            continue
        catalog = fetch_catalog(provider, cfg, get=get)
        if catalog.error:
            plan.findings.append(f"{provider}: {catalog.error}")
            continue
        plan.observations.append(
            f"{provider}: authenticated catalog contains {len(catalog.models)} models"
        )
        provider_routes = [route for route in routes if route.get("provider") == provider]
        for route in provider_routes:
            route_id = str(route.get("route_id") or "")
            model_id = _normalize_model_id(provider, str(route.get("upstream_model") or ""))
            if model_id in catalog.models:
                continue
            result = canary(provider, cfg, str(route.get("upstream_model") or ""), post=post)
            if result.classification == "missing":
                plan.removals.add(route_id)
                plan.observations.append(
                    f"{route_id}: remove after catalog absence + {result.summary}"
                )
            else:
                plan.findings.append(
                    f"{route_id}: absent from catalog; {result.classification}: {result.summary}"
                )
        if provider in NO_FREE_ROUTE_PROVIDERS:
            plan.observations.append(f"{provider}: paid/no-free policy prevents catalog additions")
            continue
        if provider not in FREE_CATALOG_PROVIDERS:
            plan.findings.append(
                f"{provider}: model list has no model-level free evidence; no automatic additions"
            )
            continue
        existing_models = {
            _normalize_model_id(provider, str(route.get("upstream_model") or ""))
            for route in provider_routes
        }
        candidates: list[tuple[str, dict[str, Any]]] = []
        seen_admission_models: set[str] = set()
        for catalog_model_id in sorted(catalog.models):
            record = catalog.models[catalog_model_id]
            if not _chat_capable(provider, record):
                plan.observations.append(
                    f"{provider}/{catalog_model_id}: skipped; catalog entry is not chat-capable"
                )
                continue
            model_id, route_record = _catalog_admission_model(
                provider, catalog_model_id, record, catalog
            )
            if model_id in existing_models or model_id in seen_admission_models:
                continue
            seen_admission_models.add(model_id)
            candidates.append((model_id, route_record))
        nvidia_catalog = fetch_nvidia_build_catalog(get=get) if provider == "nvidia" else None
        if nvidia_catalog and nvidia_catalog.error:
            plan.findings.append(
                f"nvidia: Build Catalog metadata unavailable: {nvidia_catalog.error}"
            )
        canaries_sent = 0
        for model_id, route_record in candidates:
            evidence = is_free_evidence(
                provider,
                model_id,
                route_record,
                get=get,
                nvidia_catalog=nvidia_catalog,
            )
            if evidence is None:
                plan.findings.append(
                    f"{provider}/{model_id}: free-evidence scraper was inconclusive"
                )
                continue
            if not evidence:
                continue
            quality_kwargs: dict[str, Any] = {}
            if artificial_analysis is not None:
                quality_kwargs = {
                    "artificial_analysis": artificial_analysis,
                    "huggingface_cache": huggingface_cache,
                }
            quality_result = quality(provider, model_id, route_record, get=get, **quality_kwargs)
            if quality_result.classification == "inconclusive":
                plan.findings.append(
                    f"{provider}/{model_id}: quality evidence inconclusive: "
                    f"{quality_result.summary}"
                )
                continue
            if quality_result.classification != "qualified":
                plan.observations.append(
                    f"{provider}/{model_id}: filtered by quality evidence: {quality_result.summary}"
                )
                continue
            if (
                provider == "nvidia"
                and _context_limit(
                    route_record, ("context_length", "context_window", "max_context_length")
                )
                is None
            ):
                if limits := nvidia_catalog_limits(model_id, get=get):
                    route_record = route_record | limits
            proposed = build_route(
                provider,
                cfg,
                model_id,
                route_record,
                quality_comment=_artificial_analysis_comment(
                    provider, model_id, route_record, artificial_analysis
                ),
            )
            if proposed is None:
                plan.findings.append(
                    f"{provider}/{model_id}: free but catalog lacks explicit context limit"
                )
                continue
            if proposed["route_id"] in route_ids:
                continue
            if canaries_sent >= MAX_NEW_CANARIES_PER_PROVIDER:
                plan.observations.append(
                    f"{provider}/{model_id}: eligible addition deferred by the "
                    f"{MAX_NEW_CANARIES_PER_PROVIDER}-canary safety cap"
                )
                continue
            result = canary(provider, cfg, model_id, post=post)
            canaries_sent += 1
            if result.classification == "success":
                plan.additions.append(proposed)
                route_ids.add(proposed["route_id"])
                plan.observations.append(
                    f"{proposed['route_id']}: add after free and independent quality evidence "
                    "+ canary"
                )
            else:
                plan.findings.append(
                    f"{provider}/{model_id}: free evidence but {result.classification}: "
                    f"{result.summary}"
                )
    plan.findings.extend(_missing_fallbacks(raw, plan.removals))
    return plan


def render_report(plan: Plan) -> str:
    lines = [
        "# Provider catalog reconciliation report",
        "",
        "Quality data: [Artificial Analysis](https://artificialanalysis.ai/).",
        "",
        f"Digest: `{plan.digest()[:12]}`",
        "",
    ]
    lines.extend(["## Proposed changes", ""])
    if not plan.changed():
        lines.append("No safe catalog amendment was produced.")
    else:
        for route_id in sorted(plan.removals):
            lines.append(f"- Remove `{route_id}` after explicit model-not-found evidence.")
        for route in plan.additions:
            lines.append(
                f"- Add `{route['route_id']}` after free evidence, independent quality evidence, "
                "and a successful completion canary."
            )
    lines.extend(["", "## Observations", ""])
    lines.extend([f"- {item}" for item in plan.observations] or ["- None."])
    lines.extend(["", "## Inconclusive evidence", ""])
    lines.extend([f"- {item}" for item in plan.findings] or ["- None."])
    return "\n".join(lines) + "\n"


def _route_block_bounds(text: str, route_id: str) -> tuple[int, int]:
    escaped = re.escape(route_id)
    match = re.search(rf"(?m)^  - route_id: {escaped}\s*$", text)
    if match is None:
        raise ValueError(f"could not locate route block {route_id!r}")
    # Preserve top-level comments even when they sit between the old route and its successor.
    # Their exact ownership is editorial rather than syntactic, and deleting them would erase
    # evidence a maintainer recorded for the next route.
    boundary = re.search(r"(?m)^  (?=- route_id:|#)", text[match.end() :])
    end = match.end() + boundary.start() if boundary else len(text)
    return match.start(), end


def apply_route_changes(path: Path, plan: Plan) -> None:
    """Apply only selected route blocks, preserving the document's curated comments elsewhere."""
    text = path.read_text(encoding="utf-8")
    for route_id in sorted(
        plan.removals, key=lambda item: _route_block_bounds(text, item)[0], reverse=True
    ):
        start, end = _route_block_bounds(text, route_id)
        text = text[:start] + text[end:]
    if plan.additions:
        chunks = []
        for route in plan.additions:
            serialized = route.copy()
            quality_comment = serialized.pop("_quality_comment", None)
            chunk = yaml.safe_dump([serialized], sort_keys=False).rstrip()
            if quality_comment:
                chunk = f"# Quality (informational): {quality_comment}\n{chunk}"
            chunks.append(chunk)
        added = "\n\n".join("  " + chunk.replace("\n", "\n  ") for chunk in chunks)
        text = (
            text.rstrip() + "\n\n  # Catalog-reconciled free routes (review/48).\n" + added + "\n"
        )
    path.write_text(text, encoding="utf-8")


def _run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, cwd=REPO_ROOT, text=True, capture_output=True, check=False)
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or f"command failed: {' '.join(args)}")
    return result


def _existing_issue() -> str | None:
    result = _run(
        [
            "gh",
            "issue",
            "list",
            "--state",
            "open",
            "--search",
            f'"{ISSUE_TITLE}"',
            "--json",
            "number,title",
        ],
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "could not list provider catalog issues")
    try:
        issues = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("could not parse provider catalog issue list") from exc
    for issue in issues:
        if issue.get("title") == ISSUE_TITLE:
            return str(issue["number"])
    return None


def _ensure_issue_labels() -> None:
    labels = {
        "type:operations": ("0e8a16", "Operational maintenance"),
        "area:provider": ("1d76db", "Provider integration or capacity"),
        "signal:endpoint-contract": ("fbca04", "Automated endpoint contract signal"),
        "needs:human-verification": ("d93f0b", "Requires a maintainer decision or live evidence"),
    }
    for label, (color, description) in labels.items():
        _run(
            [
                "gh",
                "label",
                "create",
                label,
                "--color",
                color,
                "--description",
                description,
                "--force",
            ]
        )


def sync_issue(plan: Plan, *, apply: bool) -> str | None:
    """Maintain exactly one issue for unsafely-automatable catalog evidence."""
    if not plan.findings:
        if apply and (existing := _existing_issue()):
            _run(
                [
                    "gh",
                    "issue",
                    "close",
                    existing,
                    "--comment",
                    "No inconclusive provider-catalog evidence remains.",
                ]
            )
        return None
    body = f"{ISSUE_MARKER}\n\n{render_report(plan)}"
    if not apply:
        print(f"dry-run: would create/update issue: {ISSUE_TITLE}")
        return None
    _ensure_issue_labels()
    existing = _existing_issue()
    label_args = [argument for label in ISSUE_LABELS for argument in ("--add-label", label)]
    if existing:
        _run(["gh", "issue", "edit", existing, "--body", body])
        _run(["gh", "issue", "edit", existing, *label_args])
        return existing
    url = _run(["gh", "issue", "create", "--title", ISSUE_TITLE, "--body", body]).stdout.strip()
    number = url.rsplit("/", 1)[-1]
    if not number.isdigit():
        raise RuntimeError(f"could not parse created issue URL: {url!r}")
    _run(["gh", "issue", "edit", number, *label_args])
    return number


def _existing_pr(branch: str) -> str | None:
    output = _run(
        [
            "gh",
            "pr",
            "list",
            "--head",
            branch,
            "--state",
            "open",
            "--json",
            "url",
            "--jq",
            ".[0].url",
        ],
        check=False,
    ).stdout.strip()
    return output or None


def checkout_pr_branch(plan: Plan) -> tuple[str | None, bool]:
    """Move to a digest branch and state whether it already contains this exact mutation."""
    if not os.environ.get("GH_TOKEN"):
        print("GH_TOKEN is unset; leaving applied changes in the working tree.", file=sys.stderr)
        return None, False
    branch = f"chore/provider-catalog-{plan.digest()[:12]}"
    _run(["gh", "auth", "setup-git"], check=False)
    fetched = _run(["git", "fetch", "origin", f"{branch}:{branch}"], check=False)
    existing = (
        fetched.returncode == 0
        or _run(["git", "rev-parse", "--verify", branch], check=False).returncode == 0
    )
    _run(["git", "checkout", branch] if existing else ["git", "checkout", "-b", branch])
    return branch, existing


def open_pr(plan: Plan, issue: str | None) -> str | None:
    """Commit the exact plan to its prepared branch and open/reuse a non-draft review PR."""
    if not os.environ.get("GH_TOKEN"):
        return None
    branch = f"chore/provider-catalog-{plan.digest()[:12]}"
    files = [
        "config/provider_limits.yml",
        "citypods/compute/llm_routes.json",
        "workers/llm-dispatch-proxy/src/dispatch_limits.json",
        "workers/llm-dispatch-v2/src/dispatch_limits.json",
    ]
    _run(["git", "add", *files])
    if _run(["git", "diff", "--cached", "--quiet"], check=False).returncode == 0:
        return _existing_pr(branch)
    _run(
        [
            "git",
            "-c",
            "user.name=github-actions[bot]",
            "-c",
            "user.email=41898282+github-actions[bot]@users.noreply.github.com",
            "commit",
            "-m",
            "chore(config): reconcile provider model catalog",
        ]
    )
    _run(["git", "push", "-u", "origin", branch])
    if existing := _existing_pr(branch):
        return existing
    body = render_report(plan)
    if issue:
        body = f"Related to #{issue}.\n\n{body}"
    return (
        _run(
            [
                "gh",
                "pr",
                "create",
                "--title",
                "chore(config): reconcile provider model catalog",
                "--body",
                body,
            ]
        ).stdout.strip()
        or None
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", action="append", default=[], help="limit to one provider")
    parser.add_argument("--apply", action="store_true", help="write only safe route amendments")
    parser.add_argument(
        "--sync-issues", action="store_true", help="create/update inconclusive evidence issue"
    )
    parser.add_argument(
        "--open-pr", action="store_true", help="commit, push, and open/reuse a review PR"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.open_pr and not args.apply:
        raise SystemExit("--open-pr requires --apply")
    raw = yaml.safe_load(LIMITS_PATH.read_text(encoding="utf-8")) or {}
    plan = plan_reconciliation(raw, set(args.provider))
    print(
        "reconciliation plan: "
        f"{len(plan.additions)} additions, {len(plan.removals)} removals, "
        f"{len(plan.findings)} inconclusives"
    )
    existing_digest_branch = False
    if args.apply and plan.changed():
        if args.open_pr:
            _, existing_digest_branch = checkout_pr_branch(plan)
        if not existing_digest_branch:
            apply_route_changes(LIMITS_PATH, plan)
            _run([sys.executable, "scripts/compile_llm_limits.py"])
    elif plan.changed():
        print("dry-run: no files changed; re-run with --apply to amend the route catalog.")
    issue = sync_issue(plan, apply=args.apply) if args.sync_issues else None
    if args.open_pr and plan.changed():
        pr_url = (
            _existing_pr(f"chore/provider-catalog-{plan.digest()[:12]}")
            if existing_digest_branch
            else open_pr(plan, issue)
        )
        if pr_url:
            print(f"pull request: {pr_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
