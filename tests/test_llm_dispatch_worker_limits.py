"""Workers Free platform limits the LLM Dispatch v2 Worker's committed config must stay inside.

Workers Free allows 64 variables per Worker, and secrets count against the same limit as the text
`vars` in wrangler.jsonc. Crossing it is invisible until deploy: #1846 declared four vars that
only restated code defaults, took the Worker to 66, and every deploy after it was rejected by the
Cloudflare API while CI stayed green. This test moves that failure to the PR.

Secrets are set out of band (`wrangler secret put`), so they are derived from what the Worker
needs: its fixed infrastructure secrets plus every provider account's `api_key_env` in
config/provider_limits.yml (gateway.js reads `env[account.api_key_env]`). Adding a provider
account therefore spends one variable too, and this test sees it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WRANGLER = REPO_ROOT / "workers" / "llm-dispatch-v2" / "wrangler.jsonc"
PROVIDER_LIMITS = REPO_ROOT / "config" / "provider_limits.yml"

WORKERS_FREE_VARIABLE_LIMIT = 64
# Documented in wrangler.jsonc's closing "Secrets" comment.
FIXED_SECRETS = {
    "BEARER_TOKEN",
    "AI_GATEWAY_AUTH_TOKEN",
    "CLOUDFLARE_ACCOUNT_ID",
    "B2_ENDPOINT",
    "B2_KEY_ID",
    "B2_APP_KEY",
    "B2_BUCKET",
}
# Headroom for a secret that is set before its config lands, or a stale one not yet deleted.
RESERVED_HEADROOM = 2


def _load_jsonc(path: Path) -> dict:
    """Parse JSONC: drop // and /* */ comments outside strings, then trailing commas."""
    text = path.read_text(encoding="utf-8")
    stripped = re.sub(
        r'("(?:[^"\\]|\\.)*")|//[^\n]*|/\*.*?\*/',
        lambda m: m.group(1) or "",
        text,
        flags=re.S,
    )
    return json.loads(re.sub(r",(\s*[}\]])", r"\1", stripped))


def _derived_secrets() -> set[str]:
    limits = yaml.safe_load(PROVIDER_LIMITS.read_text(encoding="utf-8"))
    keys = {
        account["api_key_env"]
        for provider in (limits.get("providers") or {}).values()
        for account in provider.get("accounts") or []
        if account.get("api_key_env")
    }
    return FIXED_SECRETS | keys


def test_v2_worker_stays_inside_the_workers_free_variable_limit():
    text_vars = _load_jsonc(WRANGLER).get("vars") or {}
    secrets = _derived_secrets()
    assert not set(text_vars) & secrets, "a secret must never also be a plain var"
    total = len(text_vars) + len(secrets)
    assert total + RESERVED_HEADROOM <= WORKERS_FREE_VARIABLE_LIMIT, (
        f"v2 Worker would carry {len(text_vars)} vars + {len(secrets)} secrets = {total} "
        f"(+{RESERVED_HEADROOM} headroom) > {WORKERS_FREE_VARIABLE_LIMIT}, and Cloudflare rejects "
        "the deploy. Drop vars that restate a code default before adding new ones."
    )


def test_jsonc_loader_keeps_comment_markers_inside_strings(tmp_path):
    sample = tmp_path / "sample.jsonc"
    sample.write_text('{\n  // c\n  "a": "http://x", /* b */ "b": [1,],\n}\n', encoding="utf-8")
    assert _load_jsonc(sample) == {"a": "http://x", "b": [1]}
