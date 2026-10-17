"""Which wire protocol a compiled LLM route speaks (review/53 PR1/PR2).

Routes default to OpenAI-style chat completions. A route with another ``api_shape`` (BeatAPI's JEV
``systemone`` judge endpoint) is reached only through the dispatch Worker, which builds its
requests in ``workers/llm-dispatch-v2/src/api_shapes.js``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def route_speaks_chat(route: Mapping[str, Any]) -> bool:
    """Whether a compiled route answers OpenAI-style chat completions.

    A non-chat route must never receive a chat canary, rate probe or context measurement: it cannot
    answer one, and on BeatAPI the attempt would spend the account's single shared request window.
    """
    return (route.get("api_shape") or "chat") == "chat"
