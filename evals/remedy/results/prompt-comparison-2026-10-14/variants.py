"""General prompt recipes; never inspect gold, selector rules or previous model answers."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
BODY = ROOT / "evals/remedy/results/body-selection-2026-10-13"
sys.path.insert(0, str(BODY))
from body_selection import PROMPT, messages  # noqa: E402

MODELS = ("sol", "glm_flash", "deepseek")
VARIANTS = {
    "original": PROMPT,
    "evidence_first": PROMPT
    + """
Work in this order internally before returning the same JSON schema:
1. Identify the convening body/program from recording-specific official evidence.
   Distinguish conveners from presenting staff, topics, room names and references to other bodies.
2. Assess every plausible supplied feed against its title and description. A feed can be a
   broader subscription grouping rather than an institution. A grouping assignment does not
   assert that its named committee jointly convened the recording. A real body with no dedicated
   feed may still fit an existing publication grouping if its scope and approved policy support it.
3. Return all supported memberships, with no topic-only institutional identity substitutions.
4. A player/program title alone does not establish a formal municipal convening institution.
   Distinguish a real, proven unconfigured body from an unresolved institutional owner.
Explain the evidence-to-feed connection concisely in the rationale; do not output hidden analysis.
""",
    "self_check": PROMPT
    + """
Before returning your draft, test it in both directions internally:
- For each selected feed, what official evidence proves the body or legitimate publication scope?
  Remove unsupported institutional substitutions, presenter/topic matches and invented joint bodies.
- For each plausible omitted feed, did you accidentally stop after the first institutional match?
  Consider broader subscription groupings independently of who convened the recording.
- Does empty membership mean a proven unconfigured body, an approved archive/event disposition,
  or missing owner evidence? Do not establish a formal municipal institution from a program label.
- Check that the publication action follows supplied general policy and matches selected feeds.
If the evidence supports a different interpretation, revise the draft. Do not use numerical
confidence or speculation to bridge missing proof. Return only the required JSON, with concise
cited rationale and genuine missing evidence; do not output hidden analysis.
""",
}


def request_messages(case, variant):
    payload = messages(case)
    payload[0] = {"role": "system", "content": VARIANTS[variant]}
    return payload
