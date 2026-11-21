"""Workflows that read canonical state must be able to reach its R2 manifest (#2280)."""

from __future__ import annotations

from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"
STATE_READERS = ("scripts/audit_feeds.py", "scripts/remedy_p3_daily.py")


def test_state_reading_workflows_configure_r2_coordination():
    """Without R2 the router reads a stale B2 shadow manifest and restores no source records."""
    missing = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        text = path.read_text(encoding="utf-8")
        if not any(reader in text for reader in STATE_READERS):
            continue
        if "R2_BUCKET" not in text or "R2_ACCESS_KEY_ID" not in text:
            missing.append(path.name)
    assert not missing, f"missing R2 coordination env: {missing}"
