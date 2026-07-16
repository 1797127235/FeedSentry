from __future__ import annotations

import httpx


class AppriseClient:
    def __init__(self, http: httpx.AsyncClient, base_url: str) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")

    async def notify(self, key: str, title: str, body: str) -> str:
        response = await self.http.post(
            f"{self.base_url}/notify/{key}",
            json={"title": title, "body": body, "type": "info"},
        )
        response.raise_for_status()
        return response.text[:1000]
