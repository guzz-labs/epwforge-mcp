"""Thin httpx wrapper around the EPWForge REST API.

One client instance per MCP server lifetime — reuses HTTP/2 connection pool.
Auth and base URL are read from env on construction.

v0.2.0+ — API key is OPTIONAL. The 3 read tools (find_station,
analyze_weather, chart_weather) work without auth. Only
generate_weather_file requires a key. Constructor no longer raises
when the key is missing; call sites that need it use
`require_api_key()` to surface a clear error message.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any

import httpx


DEFAULT_BASE_URL = "https://epwforge.com"
DEFAULT_TIMEOUT = 120.0  # seconds — ensemble generation can take ~60s


class EPWForgeError(RuntimeError):
    """Raised when the EPWForge API returns a non-2xx response."""

    def __init__(self, status: int, message: str, *, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


_NO_KEY_MESSAGE = (
    "EPWFORGE_API_KEY is not set. Generate one free at "
    "https://epwforge.com/account (any tier — Free signup includes 5 welcome credits) "
    "and set it in your MCP client config. Read-only tools (find_station, "
    "analyze_weather, chart_weather) work without a key; only "
    "generate_weather_file needs auth."
)


class EPWForgeClient:
    """Authenticated HTTP client for paid endpoints.

    api_key is optional at construction. `require_api_key()` raises a
    clear error if the caller depends on it being set.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.api_key = api_key or os.environ.get("EPWFORGE_API_KEY")
        self.base_url = (base_url or os.environ.get("EPWFORGE_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        headers: dict[str, str] = {"User-Agent": "epwforge-mcp"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            headers=headers,
            http2=True,
        )

    def require_api_key(self) -> None:
        """Raise EPWForgeError(401) if no API key is configured.

        Call before any tool that needs auth (generate_weather_file).
        """
        if not self.api_key:
            raise EPWForgeError(401, _NO_KEY_MESSAGE)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_bytes(self, path: str, params: dict[str, Any]) -> bytes:
        """Same auth + error semantics as get_json, but returns raw response bytes."""
        clean = {k: v for k, v in params.items() if v is not None and v != ""}
        resp = await self._client.get(path, params=clean)
        if resp.status_code == 429:
            retry = resp.headers.get("Retry-After")
            raise EPWForgeError(
                429,
                "Rate limit exceeded. Try again later.",
                retry_after=int(retry) if retry and retry.isdigit() else None,
            )
        if resp.status_code == 402:
            try:
                body = resp.json()
                msg = body.get("error", "Insufficient credits")
                if body.get("monthly_remaining") is not None:
                    msg += f" — {body['monthly_remaining']} credits remaining, this call needs {body.get('cost', '?')}"
            except Exception:
                msg = "Insufficient credits"
            raise EPWForgeError(402, f"{msg} — buy more at https://epwforge.com/pricing")
        if resp.status_code == 401:
            raise EPWForgeError(401, _NO_KEY_MESSAGE)
        if resp.status_code == 403:
            try:
                msg = resp.json().get("error", "Plan restriction")
            except Exception:
                msg = "Plan restriction"
            raise EPWForgeError(403, f"{msg} — see https://epwforge.com/pricing")
        if resp.status_code >= 400:
            try:
                msg = resp.json().get("error", resp.text[:200])
            except Exception:
                msg = resp.text[:200]
            raise EPWForgeError(resp.status_code, msg)
        return resp.content

    async def get_json(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        clean = {k: v for k, v in params.items() if v is not None and v != ""}
        resp = await self._client.get(path, params=clean)
        if resp.status_code == 429:
            retry = resp.headers.get("Retry-After")
            raise EPWForgeError(
                429,
                "Rate limit exceeded. Try again later.",
                retry_after=int(retry) if retry and retry.isdigit() else None,
            )
        if resp.status_code == 402:
            try:
                body = resp.json()
                msg = body.get("error", "Insufficient credits")
                if body.get("monthly_remaining") is not None:
                    msg += f" — {body['monthly_remaining']} credits remaining, this call needs {body.get('cost', '?')}"
            except Exception:
                msg = "Insufficient credits"
            raise EPWForgeError(402, f"{msg} — buy more at https://epwforge.com/pricing")
        if resp.status_code == 401:
            raise EPWForgeError(401, _NO_KEY_MESSAGE)
        if resp.status_code == 403:
            try:
                msg = resp.json().get("error", "Plan restriction")
            except Exception:
                msg = "Plan restriction"
            raise EPWForgeError(403, f"{msg} — see https://epwforge.com/pricing")
        if resp.status_code >= 400:
            try:
                msg = resp.json().get("error", resp.text)
            except Exception:
                msg = resp.text
            raise EPWForgeError(resp.status_code, msg)
        return resp.json()


def write_epw_base64(b64: str, save_to: str | Path) -> int:
    """Decode a base64 EPW/DDY payload and write it to disk."""
    path = Path(save_to).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = base64.b64decode(b64)
    path.write_bytes(data)
    return len(data)


async def download_text(url: str, *, timeout: float = 60.0) -> str:
    """Fetch any URL as text — used to download EPW files from public URLs."""
    async with httpx.AsyncClient(
        timeout=timeout,
        headers={"User-Agent": "epwforge-mcp"},
        follow_redirects=True,
    ) as c:
        resp = await c.get(url)
        if resp.status_code >= 400:
            snippet = resp.text[:200].replace("\n", " ")
            raise EPWForgeError(
                resp.status_code,
                f"Failed to fetch {url} (HTTP {resp.status_code}): {snippet}",
            )
        return resp.text
