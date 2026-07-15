"""Human-reviewable R12 evidence and hidden reconciliation state."""

from __future__ import annotations

import json
from base64 import urlsafe_b64decode, urlsafe_b64encode
from hashlib import sha256
from typing import Any

from citypods.discovery.models import DiscoveryResult

STATE_MARKER = "citypods:r12:state"
EVIDENCE_MARKER = "citypods:r12:evidence"


def state_marker(state: dict[str, Any]) -> str:
    """Embed a compact state ledger in a GitHub issue without exposing private inputs."""
    return f"<!-- {STATE_MARKER} {json.dumps(state, sort_keys=True, separators=(',', ':'))} -->"


def parse_state_marker(body: str) -> dict[str, Any]:
    prefix = f"<!-- {STATE_MARKER} "
    for line in body.splitlines():
        if line.startswith(prefix) and line.endswith(" -->"):
            try:
                value = json.loads(line[len(prefix) : -4])
            except json.JSONDecodeError:
                return {}
            return value if isinstance(value, dict) else {}
    return {}


def evidence_marker(result: DiscoveryResult) -> str:
    """Keep exact reviewed evidence available to the later batch workflow without a private store.

    This is not a confidentiality mechanism—the rendered comment is already public. It simply
    avoids having the batch workflow reconstruct YAML from Markdown or repeat an LLM call.
    """
    encoded = urlsafe_b64encode(
        json.dumps(result.as_dict(), sort_keys=True, separators=(",", ":")).encode()
    ).decode()
    return f"<!-- {EVIDENCE_MARKER} {encoded} -->"


def parse_evidence_marker(body: str) -> dict[str, Any] | None:
    return next(iter_evidence_markers(body), None)


def iter_evidence_markers(body: str):
    """Yield every public evidence artifact embedded in an issue or comment."""
    prefix = f"<!-- {EVIDENCE_MARKER} "
    for line in body.splitlines():
        if line.startswith(prefix) and line.endswith(" -->"):
            try:
                value = urlsafe_b64decode(line[len(prefix) : -4]).decode()
                parsed = json.loads(value)
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(parsed, dict):
                yield parsed


def evidence_digest(evidence: dict[str, Any]) -> str:
    """Return a stable approval binding for a specific public evidence artifact."""
    payload = json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
    return sha256(payload).hexdigest()


def _link(label: str, url: str | None) -> str:
    return f"- {label}: {url}" if url else f"- {label}: not found"


def _escape_reserved_markers(value: str) -> str:
    """Keep untrusted visible evidence from impersonating R12 control records."""
    return value.replace("<!-- citypods:r12:", "<!-- citypods:r12&#58;")


def render_evidence(result: DiscoveryResult) -> str:
    """Render the complete evidence bundle required before maintainer approval."""
    request = result.request
    verification = result.verification
    classification = result.classification
    heading = f"## Discovery evidence: {request.city_name}, {request.state}"
    lines = [
        heading,
        "",
        f"Mode: `{request.mode}` · confidence: `{classification.confidence}`",
        "",
    ]
    lines.extend(
        [
            _link("City website", result.city_website_url),
            _link("Meeting/listing page", result.meeting_listing_url),
            _link("Verified sample meeting", verification.sample_media_url),
            f"- Video platform: `{classification.video_platform or 'no confident match'}`",
            f"- Agenda platform: `{classification.agenda_platform or 'no confident match'}`",
            f"- Verification: {'passed' if verification.applyable else 'research finding only'}",
        ]
    )
    if verification.reason:
        lines.append(f"- Verification note: {verification.reason}")
    lines.extend(["", "### Bodies mentioned"])
    if classification.bodies_mentioned:
        lines.extend(f"- {body}" for body in classification.bodies_mentioned)
    else:
        lines.append("- None found in retrieved evidence.")
    lines.extend(["", "### Retrieved evidence"])
    for item in result.search_results:
        lines.append(f"- [{item.title or item.url}]({item.url})")
    if result.proposed_yaml:
        approval = (
            f"`/r12 approve {request.city_slug}`"
            if request.mode == "auxiliary"
            else "`/r12 approve`"
        )
        lines.extend(
            ["", "### Proposed configuration", "```yaml", result.proposed_yaml.rstrip(), "```"]
        )
        lines.extend(
            [
                "",
                f'Maintainer controls: {approval}, `/r12 reject reason="..."`, '
                "`/r12 recheck`, `/r12 batch`.",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "### Research finding only — no automatic configuration proposal",
                "The result is retained for review, but a complete provider source "
                "configuration or "
                "playable-media verification was not available. Maintainer controls: "
                "`/r12 assign-provider <provider-key> <city-slug>`, "
                '`/r12 create-provider <key> <city-slug> name="..."`, '
                "`/r12 recheck`, "
                '`/r12 defer-agenda <city-slug> until=YYYY-MM-DD reason="..."`, '
                "`/r12 clear-disposition`.",
            ]
        )
    status = "proposed" if verification.applyable else "research-only"
    lines = _escape_reserved_markers("\n".join(lines)).splitlines()
    lines.extend(
        [
            "",
            state_marker(
                {
                    "city_slug": request.city_slug,
                    "mode": request.mode,
                    "status": status,
                    "created_at": result.evidence_created_at,
                    "signature_url": verification.signature_url,
                    "platform": verification.platform,
                }
            ),
            evidence_marker(result),
        ]
    )
    return "\n".join(lines) + "\n"
