from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx


class RSSHubUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class FeedCandidate:
    title: str
    page_url: str
    route: str
    feed_url: str


class RadarMatcher:
    def discover(
        self,
        page_url: str,
        rules: Mapping[str, Any],
        base_url: str,
    ) -> list[FeedCandidate]:
        page = urlsplit(page_url)
        hostname = (page.hostname or "").lower()
        domain = self._matching_domain(hostname, rules)
        if domain is None:
            return []

        domain_rules = rules.get(domain)
        if not isinstance(domain_rules, Mapping):
            return []
        section = self._section(hostname, domain)
        candidates: list[FeedCandidate] = []
        seen: set[str] = set()
        for rule in domain_rules.get(section, []):
            if not isinstance(rule, Mapping):
                continue
            target = rule.get("target")
            sources = rule.get("source")
            if not isinstance(target, str) or not isinstance(sources, list):
                continue
            for source in sources:
                if not isinstance(source, str):
                    continue
                parameters = self._match_path(source, page.path)
                if parameters is None:
                    continue
                route = self._substitute(target, parameters)
                if route in seen:
                    break
                seen.add(route)
                candidates.append(
                    FeedCandidate(
                        title=str(rule.get("title") or route),
                        page_url=page_url,
                        route=route,
                        feed_url=self._feed_url(base_url, route),
                    )
                )
                break
        return candidates

    @staticmethod
    def _matching_domain(hostname: str, rules: Mapping[str, Any]) -> str | None:
        matches = [
            domain for domain in rules if hostname == domain or hostname.endswith(f".{domain}")
        ]
        return max(matches, key=len) if matches else None

    @staticmethod
    def _section(hostname: str, domain: str) -> str:
        if hostname == domain:
            return "www"
        return hostname.removesuffix(f".{domain}").split(".")[-1]

    @staticmethod
    def _match_path(pattern: str, path: str) -> dict[str, str] | None:
        names: list[str] = []
        parts: list[str] = []
        for segment in pattern.strip("/").split("/"):
            if segment.startswith(":"):
                names.append(segment[1:])
                parts.append(r"([^/]+)")
            elif segment == "*":
                names.append("wildcard")
                parts.append(r"(.+)")
            else:
                parts.append(re.escape(segment))
        expression = r"^/" + "/".join(parts) + r"/?$"
        match = re.fullmatch(expression, path)
        if match is None:
            return None
        return dict(zip(names, match.groups(), strict=True))

    @staticmethod
    def _substitute(target: str, parameters: Mapping[str, str]) -> str:
        route = target
        for name, value in parameters.items():
            route = route.replace(f":{name}", quote(value, safe=""))
        return route

    @staticmethod
    def _feed_url(base_url: str, route: str) -> str:
        base = urlsplit(base_url)
        path = f"{base.path.rstrip('/')}/{route.lstrip('/')}"
        return urlunsplit((base.scheme, base.netloc, path, "", ""))


class CandidateCodec:
    def __init__(
        self,
        secret: bytes,
        *,
        clock: Callable[[], datetime] | None = None,
        ttl: timedelta = timedelta(minutes=10),
    ) -> None:
        self.secret = secret
        self.clock = clock or (lambda: datetime.now(UTC))
        self.ttl = ttl

    def encode(self, candidate: FeedCandidate) -> str:
        payload = {**asdict(candidate), "expires_at": (self.clock() + self.ttl).timestamp()}
        encoded = self._encode_json(payload)
        signature = hmac.new(self.secret, encoded, hashlib.sha256).digest()
        return f"{self._b64(encoded)}.{self._b64(signature)}"

    def decode(self, token: str) -> FeedCandidate:
        try:
            payload_part, signature_part = token.split(".", 1)
            payload_bytes = self._unb64(payload_part)
            supplied_signature = self._unb64(signature_part)
        except (ValueError, TypeError) as exc:
            raise ValueError("invalid candidate") from exc
        expected_signature = hmac.new(self.secret, payload_bytes, hashlib.sha256).digest()
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise ValueError("invalid candidate")
        try:
            payload = json.loads(payload_bytes)
            expires_at = float(payload.pop("expires_at"))
            candidate = FeedCandidate(**payload)
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError("invalid candidate") from exc
        if self.clock().timestamp() > expires_at:
            raise ValueError("candidate expired")
        return candidate

    @staticmethod
    def _encode_json(payload: Mapping[str, Any]) -> bytes:
        return json.dumps(
            payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        ).encode()

    @staticmethod
    def _b64(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    @staticmethod
    def _unb64(value: str) -> bytes:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class RSSHubClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        base_url: str,
        *,
        clock: Callable[[], datetime] | None = None,
        cache_ttl: timedelta = timedelta(hours=1),
        max_rules_bytes: int = 2_000_000,
    ) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.clock = clock or (lambda: datetime.now(UTC))
        self.cache_ttl = cache_ttl
        self.max_rules_bytes = max_rules_bytes
        self._rules: dict[str, Any] | None = None
        self._loaded_at: datetime | None = None

    async def rules(self) -> dict[str, Any]:
        if self._cache_is_fresh():
            return self._rules or {}
        try:
            response = await self.http.get(f"{self.base_url}/api/radar/rules")
            response.raise_for_status()
            if len(response.content) > self.max_rules_bytes:
                raise ValueError("RSSHub Radar rules response is too large")
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("RSSHub Radar rules must be an object")
        except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
            if self._rules is not None:
                return self._rules
            raise RSSHubUnavailable("RSSHub Radar rules are unavailable") from exc
        self._rules = payload
        self._loaded_at = self.clock()
        return payload

    def _cache_is_fresh(self) -> bool:
        return (
            self._rules is not None
            and self._loaded_at is not None
            and self.clock() - self._loaded_at < self.cache_ttl
        )
