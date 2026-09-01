"""Fail-closed bridge from YmaKmern replies to meme_manager.

The bridge never asks meme_manager to rewrite a reply or inject a persona
prompt.  It selects at most one reviewed category marker, lets the public
compatibility API remove that internal marker, then sends the prepared image
after AstrBot has delivered the text.
"""
from __future__ import annotations

import hashlib
import os
import re
import time


_ADAPTER_VERSION = "ymakmern-meme-bridge/1.0"
_HIGH_RISK_RE = re.compile(
    r"(?:自杀|轻生|想死|急救|胸痛|呼吸困难|处方|用药|起诉|律师|"
    r"投资|转账|诈骗|银行卡|人身安全)"
)

_TAG_CUES = (
    (("早安", "早上好", "早啊"), ("morning", "happy")),
    (("晚安", "睡觉", "困了", "休息"), ("sleep", "sigh")),
    (("恭喜", "成功", "好耶", "开心", "哈哈", "笑死", "太棒"),
     ("happy", "like")),
    (("喜欢", "好看", "可爱", "真棒", "不错"), ("like", "happy")),
    (("谢谢", "夸", "厉害", "真会"), ("shy", "happy")),
    (("没看懂", "不明白", "什么情况", "怎么回事"), ("confused",)),
    (("无语", "没办法", "又来", "离谱", "唉"), ("sigh", "baka")),
    (("猫", "喵"), ("meow", "like")),
    (("代码", "程序", "模型", "服务器", "bug"), ("cpu",)),
    (("笨", "傻", "这都", "服了"), ("baka", "fool")),
)


def _event_value(event, name: str) -> str:
    try:
        value = event.get_extra(name)
        if value is not None:
            return str(value)
    except Exception:
        pass
    return str(getattr(event, name, "") or "")


def _group_id(event) -> str:
    group = getattr(getattr(event, "message_obj", None), "group", None)
    return str(group or getattr(event, "group_id", "") or "").strip()


def _message_id(event) -> str:
    message_obj = getattr(event, "message_obj", None)
    return str(
        getattr(message_obj, "message_id", "")
        or getattr(event, "message_id", "")
        or _event_value(event, "dududa_policy_run_id")
        or id(event)
    )


class MemeManagerAdapter:
    """Small AstrBot-specific delivery adapter with deterministic limits."""

    def __init__(self, context):
        self._context = context
        self._last_sent: dict[str, float] = {}
        self._pending: dict[str, tuple[object, dict]] = {}

    @staticmethod
    def _enabled_groups() -> set[str]:
        return {
            item.strip() for item in os.environ.get(
                "DUDUDA_MEME_MANAGER_GROUPS", "").split(",")
            if item.strip()
        }

    @staticmethod
    def _rate() -> int:
        try:
            return min(100, max(0, int(os.environ.get(
                "DUDUDA_MEME_MANAGER_RATE", "10"))))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _cooldown() -> float:
        try:
            return max(0.0, float(os.environ.get(
                "DUDUDA_MEME_MANAGER_COOLDOWN", "600")))
        except (TypeError, ValueError):
            return 600.0

    def _manager(self):
        try:
            metadata = self._context.get_registered_star("meme_manager")
            manager = metadata.star_cls if metadata else None
            if manager and callable(getattr(
                    manager, "compat_prepare_message", None)):
                return manager
        except Exception:
            pass
        return None

    def _eligible(self, event, reply: str, now: float) -> bool:
        if os.environ.get("DUDUDA_MEME_MANAGER", "0") != "1":
            return False
        group_id = _group_id(event)
        if not group_id or group_id not in self._enabled_groups():
            return False
        if _HIGH_RISK_RE.search(
                f"{getattr(event, 'message_str', '')}\n{reply}"):
            return False
        origin = _event_value(event, "dududa_response_origin")
        if origin in {
                "tool", "progress", "command", "subscription",
                "user_cancelled", "system_error"}:
            return False
        if now - self._last_sent.get(group_id, 0.0) < self._cooldown():
            return False
        rate = self._rate()
        if rate <= 0:
            return False
        seed = hashlib.sha256(
            f"{_ADAPTER_VERSION}|{_message_id(event)}".encode("utf-8")
        ).digest()
        return int.from_bytes(seed[:2], "big") % 100 < rate

    @staticmethod
    def _select_tag(manager, event, reply: str) -> str:
        mapping = getattr(manager, "category_mapping", {}) or {}
        try:
            context = manager._resolve_runtime_pack_context(event=event)
            candidate = context.get("category_mapping")
            if isinstance(candidate, dict):
                mapping = candidate
        except Exception:
            pass
        available = {str(key).strip() for key in mapping if str(key).strip()}
        if not available:
            return ""
        visible = str(reply or "")
        incoming = str(getattr(event, "message_str", "") or "")
        combined = f"{incoming}\n{visible}"
        for cues, preferred_tags in _TAG_CUES:
            if any(cue in combined for cue in cues):
                for tag in preferred_tags:
                    if tag in available:
                        return tag
        return ""

    async def prepare_result(self, event, reply: str):
        """Return an AstrBot result and retain only a pending prepared image."""
        text = str(reply or "")
        manager = self._manager()
        now = time.monotonic()
        if manager is None or not self._eligible(event, text, now):
            return event.plain_result(text)
        tag = self._select_tag(manager, event, text)
        if not tag:
            return event.plain_result(text)
        try:
            prepared = await manager.compat_prepare_message(
                event, f"{text}\n&&{tag}&&")
        except Exception:
            return event.plain_result(text)
        images = prepared.get("images") or []
        if not images:
            return event.plain_result(text)
        key = _message_id(event)
        self._pending[key] = (manager, prepared)
        self._last_sent[_group_id(event)] = now
        cleaned = prepared.get("cleaned_chain")
        if cleaned is not None and getattr(cleaned, "chain", None):
            try:
                return event.chain_result(cleaned.chain)
            except Exception:
                pass
        return event.plain_result(text)

    async def flush_after_text(self, event) -> int:
        """Send a prepared image once; repeated callbacks are harmless."""
        item = self._pending.pop(_message_id(event), None)
        if item is None:
            return 0
        manager, prepared = item
        try:
            result = await manager.compat_send_prepared_message(
                event, prepared, send_text=False, send_images=True)
            return int(result.get("sent_images_count", 0) or 0)
        except Exception:
            return 0
