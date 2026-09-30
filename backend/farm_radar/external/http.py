"""外部の公開API（認証なし・読み取りだけ）を呼ぶための小さな土台。

- 1回ごとに間隔をあける（回数制限を守るため）
- 429（回数制限）や 5xx は、待ってから再試行する
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

import httpx

from .. import ratelimit

log = logging.getLogger(__name__)

USER_AGENT = "farm-radar/0.1 (read-only)"


class ExternalError(Exception):
    """外部APIから使える応答が得られなかった。"""


class JsonGetter:
    def __init__(
        self,
        base_url: str,
        *,
        min_interval: float = 0.0,
        max_retries: int = 3,
        backoff_seconds: float = 5.0,
        timeout: float = 20.0,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.base_url = base_url.rstrip("/")
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self._client = client or httpx.Client(timeout=timeout, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
        self._sleep = sleep
        self._clock = clock
        self._host = ratelimit.host_of(self.base_url)

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """JSON を返すAPIを読む。"""
        return self._fetch(path, params).json()

    def get_text(self, path: str, params: dict[str, Any] | None = None) -> str:
        """ドキュメントのページなど、本文をそのまま読む。"""
        return self._fetch(path, params).text

    def _fetch(self, path: str, params: dict[str, Any] | None) -> httpx.Response:
        url = path if path.startswith("https://") else f"{self.base_url}/{path.lstrip('/')}"
        last_error = ""
        host = ratelimit.host_of(url) if url.startswith("https://") else self._host
        for attempt in range(self.max_retries + 1):
            # 同じサイトへの呼び出しは、ほかの読み手（別の会場・ジョブ）とも間隔を共有する（M6）
            ratelimit.wait_turn(host, self.min_interval, self._sleep, self._clock)
            try:
                resp = self._client.get(url, params=params)
            except httpx.HTTPError as exc:
                last_error = str(exc)
            else:
                if resp.status_code == 200:
                    return resp
                last_error = f"HTTP {resp.status_code}"
                if resp.status_code == 429:
                    ratelimit.record(host, "external")
                if resp.status_code not in (429, 500, 502, 503, 504):
                    break
            if attempt < self.max_retries:
                self._sleep(self.backoff_seconds * (2 ** attempt))
        log.warning("external api failed", extra={"data": {"url": url, "error": last_error}})
        raise ExternalError(f"{url}: {last_error}")
