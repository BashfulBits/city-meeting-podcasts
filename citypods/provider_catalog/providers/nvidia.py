"""NVIDIA API Catalog (integrate.api.nvidia.com).

Free evidence is hybrid (live evidence 2026-09-24, tests/provider_catalog/evidence_2026_09_24.json):
`/v1/models` lists 82 ids, but 54 have no Build Catalog card and every sampled one returned 404
"Function ... Not found for account" -- listed, not served. The public NGC search API labels the
free endpoints ("Free Endpoint"). A 200 proves a model is *served* on our key, not what it costs:
two models WITHOUT the label (nemotron-parse-2.0, nemoguard content-safety -- both non-chat) also
returned 200, and NVIDIA's responses carry no billing signal. So the label is required as the
conservative "NVIDIA itself calls this free" rule (every labeled chat model sampled served), and
the canary proves it is served. First bytes took up to 300 s: never time out early.
"""

from __future__ import annotations

import json
import re
from typing import Any

from citypods.provider_catalog.rules import (
    END_OF_LIFE,
    ProviderRules,
    Signal,
    body_matches,
)

NGC_SEARCH = "https://api.ngc.nvidia.com/v2/search/catalog/resources/ENDPOINT"
BUILD_ORG = "qc69jvmznzxy"


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def build_catalog_labels(session: Any) -> dict[str, Any]:
    """Read every Build Catalog endpoint card once per run: {'publisher/slug': is_free}."""
    cards: dict[str, bool] = {}
    page = 0
    try:
        while True:
            query = json.dumps(
                {
                    "query": "*",
                    "filters": [{"field": "orgName", "value": BUILD_ORG}],
                    "page": page,
                    "pageSize": 100,
                },
                separators=(",", ":"),
            )
            response = session.get(NGC_SEARCH, params={"q": query}, timeout=30)
            if not response.ok:
                return {"error": f"Build Catalog HTTP {response.status_code}"}
            payload = response.json()
            seen = 0
            for group in payload.get("results") or []:
                for resource in group.get("resources") or []:
                    seen += 1
                    publisher, labels = None, set()
                    for label in resource.get("labels") or []:
                        values = label.get("values") or []
                        if label.get("key") == "publisher" and len(values) == 1:
                            publisher = values[0]
                        labels.update(str(v) for v in values)
                    if publisher and resource.get("name"):
                        cards[f"{publisher}/{_slug(resource['name'])}"] = "Free Endpoint" in labels
            page += 1
            if seen == 0 or page * 100 >= int(payload.get("resultTotal") or 0):
                return {"cards": cards}
    except Exception as exc:  # noqa: BLE001 -- an unavailable scraper is inconclusive, not "paid"
        return {"error": f"Build Catalog {type(exc).__name__}"}


def free_endpoint_label(model: str, _record: Any, ctx: Any) -> bool | None:
    if ctx.get("error"):
        return None
    publisher, _, name = model.partition("/")
    # No card at all is a dead listing (skip silently); a card without the label is not free.
    return bool(ctx.get("cards", {}).get(f"{publisher}/{_slug(name)}"))


def _links(model: str) -> tuple[tuple[str, str], ...]:
    publisher, _, name = model.partition("/")
    return (
        ("NVIDIA Build", f"https://build.nvidia.com/{publisher}/{_slug(name)}"),
        ("Hugging Face search", f"https://huggingface.co/models?search={name}"),
        ("Artificial Analysis models", "https://artificialanalysis.ai/models"),
    )


RULES = ProviderRules(
    name="nvidia",
    free_evidence=free_endpoint_label,
    prepare=build_catalog_labels,
    research_links=_links,
    signals=(
        END_OF_LIFE,
        Signal(
            "not_served",
            404,
            body_matches(r"function '[^']*': not found for account"),
            "404 function not found for account (listed but not served)",
        ),
    ),
)
