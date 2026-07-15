"""Small, bounded Tavily client used before—not inside—LLM classification."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import requests

from citypods.discovery.models import DiscoveryRequest, SearchResult

TAVILY_SEARCH_URL = "https://api.tavily.com/search"


class TavilySearchError(RuntimeError):
    """A safe error that never exposes an API key or raw provider response."""


@dataclass(frozen=True)
class TavilyConfig:
    api_key: str
    max_results: int = 8
    timeout_seconds: float = 20.0

    @classmethod
    def from_env(cls) -> TavilyConfig:
        key = os.environ.get("TAVILY_API_KEY", "").strip()
        if not key:
            raise TavilySearchError("TAVILY_API_KEY is required for city discovery")
        return cls(api_key=key)


class TavilyClient:
    """Search Tavily's known API host; later fetch result URLs through SSRF guards."""

    def __init__(
        self, config: TavilyConfig | None = None, *, session: requests.Session | None = None
    ) -> None:
        self.config = config or TavilyConfig.from_env()
        self._session = session or requests.Session()

    @staticmethod
    def query_for(request: DiscoveryRequest) -> str:
        pieces = [f"{request.city_name}, {request.state}", "public meetings"]
        if request.mode == "auxiliary":
            pieces.append("agenda minutes portal")
        else:
            pieces.append("city council video meeting archive")
        if request.meeting_url_hint:
            pieces.append(request.meeting_url_hint)
        if request.provider_hint and request.provider_hint.lower() not in {"not sure", "other"}:
            pieces.append(request.provider_hint)
        return " ".join(pieces)

    def search(self, request: DiscoveryRequest) -> list[SearchResult]:
        payload = {
            "api_key": self.config.api_key,
            "query": self.query_for(request),
            "search_depth": "basic",
            "max_results": min(max(1, self.config.max_results), 10),
            "include_answer": False,
            "include_raw_content": False,
        }
        try:
            response = self._session.post(
                TAVILY_SEARCH_URL, json=payload, timeout=self.config.timeout_seconds
            )
        except requests.RequestException as exc:
            raise TavilySearchError("Tavily search request failed") from exc
        if response.status_code != 200:
            raise TavilySearchError(f"Tavily search returned HTTP {response.status_code}")
        try:
            body: Any = response.json()
            rows = body.get("results", []) if isinstance(body, dict) else []
        except ValueError as exc:
            raise TavilySearchError("Tavily search returned invalid JSON") from exc
        results: list[SearchResult] = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("url"), str):
                continue
            results.append(
                SearchResult(
                    url=row["url"],
                    title=str(row.get("title", "")),
                    content=str(row.get("content", "")),
                    score=(
                        float(row["score"]) if isinstance(row.get("score"), (int, float)) else None
                    ),
                )
            )
        return results
