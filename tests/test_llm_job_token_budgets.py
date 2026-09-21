"""Static audit: every structured-output LLM job must declare its own output token budget.

chapter-agenda, chapter-locator, and the topic-tags prelabeler were all dispatched with no
explicit ``max_tokens``, silently falling back to ``LiteLLMBackend``'s generic 1024-token
``DEFAULT_OUTPUT_TOKEN_MARGIN`` -- far below what a multi-item structured response actually needs.
Responses were routinely truncated mid-JSON, producing near-100% validation-error rates in
production before anyone noticed (see CHANGELOG.md).

``LiteLLMBackend._output_token_budget`` can't be made to *raise* on a missing ``max_tokens``: a
large number of existing tests construct ``structured_output`` jobs without one, deliberately,
to exercise unrelated behavior (routing, retries, dispatch-only-key leakage, ...), and forcing an
explicit budget on every one of those fixtures is out of scope here and would only churn unrelated
tests. This module instead audits the actual *production* call sites statically -- every place
``citypods/*.py`` builds an ``InferenceJob`` (or an inline job payload dict) with a
``"structured_output"`` key must set ``"max_tokens"`` in the same dict literal. New lanes are
caught here at test time, the same way a bug in a new lane's ``max_tokens`` would previously have
gone unnoticed until it silently truncated a production response.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Directories under `citypods/` intentionally not scanned: nothing in them constructs a real
# production LLM job today, so a `structured_output` literal here would be test-only fixture code.
_EXCLUDED_DIR_NAMES = {"__pycache__"}


def _iter_source_files():
    for path in sorted((REPO_ROOT / "citypods").rglob("*.py")):
        if not any(part in _EXCLUDED_DIR_NAMES for part in path.parts):
            yield path


def _dict_literal_span(text: str, key_index: int) -> tuple[int, int] | None:
    """Return the ``(start, end)`` span of the ``{...}`` dict literal enclosing ``key_index``.

    Walks left from the key to the nearest unmatched ``{``, then right from there to its matching
    ``}`` via simple brace counting. Good enough for this codebase's inline dict literals (no
    braces inside string values on the scanned lines) without pulling in a full Python parser.
    """
    depth = 0
    start = None
    i = key_index
    while i >= 0:
        char = text[i]
        if char == "}":
            depth += 1
        elif char == "{":
            if depth == 0:
                start = i
                break
            depth -= 1
        i -= 1
    if start is None:
        return None
    depth = 0
    j = start
    while j < len(text):
        char = text[j]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return start, j + 1
        j += 1
    return None



# Only a `"structured_output":` dict-KEY occurrence is a candidate job-payload literal -- this
# deliberately excludes reads like `job.inputs.get("structured_output")` and keyword arguments
# like `structured_output=data.get(...)` (JobHandle/JobResult deserialization), neither of which
# is a dict literal at all.
_KEY_PATTERN = '"structured_output":'


def _structured_output_sites_missing_max_tokens() -> list[str]:
    violations: list[str] = []
    for path in _iter_source_files():
        text = path.read_text(encoding="utf-8")
        search_from = 0
        while True:
            key_index = text.find(_KEY_PATTERN, search_from)
            if key_index == -1:
                break
            search_from = key_index + len(_KEY_PATTERN)
            span = _dict_literal_span(text, key_index)
            line_no = text.count("\n", 0, key_index) + 1
            if span is None:
                violations.append(
                    f"{path.relative_to(REPO_ROOT)}:{line_no}: could not locate the enclosing "
                    "dict literal for this 'structured_output' key (update this audit's brace-"
                    "matching heuristic, or confirm by hand that max_tokens is set alongside it)"
                )
                continue
            start, end = span
            literal = text[start:end]
            if '"messages"' not in literal:
                # Not an outbound job-request payload -- e.g. llm_deferred.py's registry-record
                # serialization dict, which persists `structured_output` as plain state but builds
                # `messages` (if any) via a separate follow-up assignment, not this literal.
                continue
            if '"max_tokens"' not in literal:
                violations.append(
                    f"{path.relative_to(REPO_ROOT)}:{line_no}: a job payload sets both 'messages' "
                    "and 'structured_output' but no 'max_tokens' in the same dict literal -- this "
                    "job would silently fall back to LiteLLMBackend.DEFAULT_OUTPUT_TOKEN_MARGIN "
                    "(1024 tokens), which is almost never enough for a multi-field/multi-item "
                    "structured response. Set an explicit, schema-appropriate max_tokens (see "
                    "AGENDA_OUTPUT_TOKEN_BUDGET / LOCATOR_OUTPUT_TOKEN_RESERVE / "
                    "JUDGE_OUTPUT_TOKEN_BUDGET for examples)."
                )
    return violations


def test_every_structured_output_job_declares_an_explicit_max_tokens():
    violations = _structured_output_sites_missing_max_tokens()
    assert not violations, "\n" + "\n".join(violations)


def test_audit_actually_inspects_at_least_the_known_production_call_sites():
    """Guard the audit itself against silently scanning zero files (e.g. a bad glob after a
    directory rename) by requiring it to have found every currently-known call site."""
    known_files = {
        "citypods/audit_remedy.py",
        "citypods/chapter_jobs.py",
        "citypods/discovery/classify.py",
        "citypods/stages.py",
        "citypods/tags.py",
        "citypods/tournament.py",
    }
    scanned = {str(path.relative_to(REPO_ROOT)) for path in _iter_source_files()}
    missing = known_files - scanned
    assert not missing, f"audit did not scan expected files: {sorted(missing)}"
