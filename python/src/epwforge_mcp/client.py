"""Thin httpx wrapper around the EPWForge REST API.

One client instance per MCP server lifetime — reuses HTTP/2 connection pool.
Auth and base URL are read from env on construction.
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


class EPWForgeClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.api_key = api_key or os.environ.get("EPWFORGE_API_KEY")
        self.base_url = (base_url or os.environ.get("EPWFORGE_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        if not self.api_key:
            raise RuntimeError(
                "EPWFORGE_API_KEY is not set. Generate a key at "
                "https://epwforge.com/account and set it in your MCP client config."
            )
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": "epwforge-mcp",
            },
            http2=True,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

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
        if resp.status_code == 403:
            try:
                msg = resp.json().get("error", "Plan upgrade required")
            except Exception:
                msg = "Plan upgrade required"
            raise EPWForgeError(
                403,
                f"{msg} — upgrade at https://epwforge.com/pricing",
            )
        if resp.status_code >= 400:
            try:
                msg = resp.json().get("error", resp.text)
            except Exception:
                msg = resp.text
            raise EPWForgeError(resp.status_code, msg)
        return resp.json()


def write_epw_base64(b64: str, save_to: str | Path) -> int:
    """Decode a base64 EPW/DDY payload and write it to disk.

    Returns bytes written. Parent directory is created if missing.
    """
    path = Path(save_to).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = base64.b64decode(b64)
    path.write_bytes(data)
    return len(data)
