from __future__ import annotations

from typing import Any

import httpx


class FirecrawlClient:
    def __init__(self, http: httpx.AsyncClient, base_url: str, api_key: str | None) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    async def scrape(self, url: str) -> str:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        response = await self.http.post(
            f"{self.base_url}/v1/scrape",
            headers=headers,
            json={"url": url, "formats": ["markdown"], "onlyMainContent": True},
        )
        response.raise_for_status()
        payload: Any = response.json()
        try:
            markdown = payload["data"]["markdown"]
        except (KeyError, TypeError) as exc:
            raise ValueError("Firecrawl response did not contain markdown") from exc
        if not isinstance(markdown, str) or not markdown.strip():
            raise ValueError("Firecrawl response did not contain markdown")
        return markdown
