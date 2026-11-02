"""Short, honest facts about a candidate model, for a human deciding whether to add it.

Artificial Analysis often has no entry for a brand-new model, so the issue also shows what the
sources a person would open anyway can say: the provider's own catalog record (OpenRouter and
Kilo carry a description, release time and expiry), the NVIDIA Build model card, and Hugging
Face metadata. Everything here is best effort and display-only: it never gates a decision, and
every value is untrusted text, so it is reduced to short plain text before it is stored.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any

import requests

from citypods.security import SecurityError, validate_source_url

MAX_TEXT = 220
_SIZE = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)([bt])(?:-a(\d+(?:\.\d+)?)b)?(?![a-z0-9])", re.I)
HF_SEARCH = "https://huggingface.co/api/models"
# NVIDIA Build cards open with legal/hardware boilerplate; none of it describes the model.
_BOILERPLATE = re.compile(
    r"GPU-accelerated|designed and/or optimized|governing terms|terms of use|"
    r"ready for commercial|not owned or developed by NVIDIA|trial service|license",
    re.I,
)


def plain(value: Any, limit: int = MAX_TEXT) -> str:
    """One line of inert text: no markup, links, mentions or control characters."""
    text = re.sub(r"\s+", " ", str(value or ""))
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\s+", " ", re.sub(r"[<>`|\[\]@*_~#\\]", " ", text)).strip()
    if len(text) > limit:
        text = text[: limit - 1].rsplit(" ", 1)[0] + "…"
    return text


def _size_from_name(model: str) -> str | None:
    match = _SIZE.search(model.rsplit("/", 1)[-1].replace("_", "-"))
    if not match:
        return None
    total, unit, active = match.group(1), match.group(2).upper(), match.group(3)
    return f"{total}{unit}" + (f" ({active}B active)" if active else "")


def _day(value: Any, *, future_ok: bool = False) -> str | None:
    """An ISO date, only if it is a plausible release date.

    NVIDIA's catalog reports `created: 735790403` for every model (1993-04-26), so a date before
    2015 or after tomorrow is a placeholder, not a fact.
    """
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            day = datetime.fromtimestamp(float(value), UTC).date()
        elif isinstance(value, str):
            day = date.fromisoformat(value[:10])
        else:
            return None
    except (ValueError, OverflowError, OSError):
        return None
    if day < date(2015, 1, 1):
        return None
    if not future_ok and day > datetime.now(UTC).date() + timedelta(days=1):
        return None
    return day.isoformat()


def catalog_facts(model: str, record: Mapping[str, Any]) -> dict[str, str]:
    """Facts already in the provider's catalog record; no request is made."""
    facts: dict[str, str] = {}
    description = plain(record.get("description"))
    if description:
        facts["summary"] = description
    if size := _size_from_name(model):
        facts["size"] = size
    if released := _day(record.get("created")):
        facts["released"] = released
    if expires := _day(record.get("expiration_date"), future_ok=True):
        facts["expires"] = expires
    architecture = record.get("architecture")
    if isinstance(architecture, Mapping):
        modalities = architecture.get("input_modalities")
        if isinstance(modalities, list):
            names = [plain(m, 16) for m in modalities if isinstance(m, str)]
            if names:
                facts["inputs"] = ", ".join(names[:5])
    return facts


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower().removesuffix(":free"))


def hugging_face_facts(session: requests.Session, model: str) -> dict[str, str]:
    """Look the model up by name on Hugging Face; accept only a single unambiguous match."""
    name = model.rsplit("/", 1)[-1].removesuffix(":free")
    try:
        response = session.get(
            HF_SEARCH,
            params={"search": name, "limit": 8, "sort": "downloads", "full": "true"},
            timeout=10,
        )
        rows = response.json() if response.ok else []
    except (requests.RequestException, ValueError):
        return {}
    if not isinstance(rows, list):
        return {}
    wanted = _slug(name)
    hits = [
        r
        for r in rows
        if isinstance(r, dict)
        and isinstance(r.get("id"), str)
        and _slug(r["id"].rsplit("/", 1)[-1]) == wanted
    ]
    if len(hits) != 1:
        return {}
    hit = hits[0]
    facts: dict[str, str] = {}
    repo = hit["id"]
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        facts["card"] = f"https://huggingface.co/{repo}"
    if released := _day(hit.get("createdAt")):
        facts["released"] = released
    total = (
        (hit.get("safetensors") or {}).get("total")
        if isinstance(hit.get("safetensors"), Mapping)
        else None
    )
    if type(total) is int and total > 0:
        facts["params"] = f"{total / 1e9:.1f}B parameters"
    tags = hit.get("tags") if isinstance(hit.get("tags"), list) else []
    licenses = [t[8:] for t in tags if isinstance(t, str) and t.startswith("license:")]
    if licenses:
        facts["license"] = plain(licenses[0], 40)
    return facts


def nvidia_card_summary(session: requests.Session, links) -> str:
    """First descriptive paragraph of an NVIDIA Build model card (markdown), if there is one."""
    url = next((u for label, u in links if label == "NVIDIA Build"), None)
    if not url:
        return ""
    card = url.rstrip("/") + ".md"
    try:
        # The URL carries a provider-supplied model ID: fixed host, public address, no redirects.
        validate_source_url(card, allowed_hosts=["build.nvidia.com"])
        response = session.get(card, timeout=10, allow_redirects=False)
        text = response.text[:20_000] if response.ok else ""
    except (SecurityError, requests.RequestException, ValueError, AttributeError, TypeError):
        return ""
    for block in re.split(r"\n\s*\n", text):
        stripped = block.strip()
        if (
            len(stripped) >= 80
            and not stripped.startswith(("#", "|", "-", "*", ">", "!", "["))
            and not _BOILERPLATE.search(stripped)
        ):
            return plain(stripped)
    return ""


def gather_facts(
    session: requests.Session, provider: str, model: str, record: Mapping[str, Any], links
) -> dict[str, str]:
    """Merge catalog, NVIDIA card and Hugging Face facts; earlier sources win on conflicts."""
    facts = catalog_facts(model, record)
    if "summary" not in facts and provider == "nvidia":
        summary = nvidia_card_summary(session, links)
        if summary:
            facts["summary"] = summary
    for key, value in hugging_face_facts(session, model).items():
        facts.setdefault(key, value)
    return facts
