"""Telegram のボット（送信と、オーナーからのコマンドの受け取り）。

- トークンは .env の TELEGRAM_BOT_TOKEN からだけ読む。ログ・画面・データベースには出さない。
- 受け付けるのは .env の TELEGRAM_OWNER_ID の人だけ（SPEC 12.5章）。ほかの人のメッセージは無視する。
- このボットは読み取り専用のシステムの「お知らせ係」。お金を動かす機能は持たない。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)

API = "https://api.telegram.org"
HELP_COMMANDS = ["/status", "/report", "/stop", "/exit_all", "/resume"]
MAX_TEXT = 4000          # Telegram の1通の上限は4096文字。少し余裕をもたせる
_TOKEN_RE = re.compile(r"\d{5,}:[A-Za-z0-9_\-]{20,}")


def hide_token(text: str) -> str:
    """文字列の中にボットのトークンがあれば *** に置きかえる（エラーの文に URL が入ることがあるため）。"""
    return _TOKEN_RE.sub("***", text)


@dataclass(frozen=True)
class TelegramSettings:
    token: str
    owner_id: int

    def __repr__(self) -> str:          # うっかり print してもトークンが出ないように
        return f"TelegramSettings(owner_id={self.owner_id}, token=***)"


def settings_from_env(env: Mapping[str, str]) -> TelegramSettings | None:
    """.env の設定を読む。どちらかが空なら None（通知は送らず、記録だけする）。"""
    token = (env.get("TELEGRAM_BOT_TOKEN") or "").strip()
    owner = (env.get("TELEGRAM_OWNER_ID") or "").strip()
    if not token or not owner:
        return None
    if not _TOKEN_RE.fullmatch(token):
        log.error("TELEGRAM_BOT_TOKEN の形がちがいます（数字:英数字 の形のはずです）。通知は送りません。")
        return None
    try:
        owner_id = int(owner)
    except ValueError:
        log.error("TELEGRAM_OWNER_ID は数字だけにしてください。通知は送りません。")
        return None
    return TelegramSettings(token, owner_id)


class TelegramError(Exception):
    """Telegram に送れなかった。メッセージにトークンは入れない。"""


class Telegram:
    def __init__(self, settings: TelegramSettings, client: httpx.Client | None = None, timeout: float = 20.0):
        self._s = settings
        self._client = client or httpx.Client(timeout=timeout)

    @property
    def owner_id(self) -> int:
        return self._s.owner_id

    def _call(self, method: str, payload: dict[str, Any], timeout: float | None = None) -> Any:
        url = f"{API}/bot{self._s.token}/{method}"
        try:
            resp = self._client.post(url, json=payload, timeout=timeout)
            data = resp.json()
        except Exception as exc:  # 通信の失敗。例外の文に URL（トークン入り）が入ることがあるので隠す
            raise TelegramError(hide_token(f"{method}: {type(exc).__name__}: {exc}")) from None
        if not data.get("ok"):
            raise TelegramError(hide_token(f"{method}: {data.get('error_code')} {data.get('description')}"))
        return data.get("result")

    def send(self, text: str) -> None:
        """オーナーに送る。長いときは分けて送る。"""
        for part in split_text(text):
            self._call("sendMessage", {"chat_id": self._s.owner_id, "text": part,
                                       "disable_web_page_preview": True})

    def updates(self, offset: int | None, wait_seconds: int = 25) -> list[dict[str, Any]]:
        """新しく届いたメッセージ（ロングポーリング: 最大 wait_seconds 秒待つ）。"""
        payload: dict[str, Any] = {"timeout": wait_seconds, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        return self._call("getUpdates", payload, timeout=wait_seconds + 10) or []


def split_text(text: str, limit: int = MAX_TEXT) -> list[str]:
    """行の区切りで limit 文字以下に分ける。"""
    parts, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(line[:limit])
            line = line[limit:]
        cand = f"{cur}\n{line}" if cur else line
        if len(cand) > limit:
            parts.append(cur)
            cur = line
        else:
            cur = cand
    if cur:
        parts.append(cur)
    return parts or [""]
