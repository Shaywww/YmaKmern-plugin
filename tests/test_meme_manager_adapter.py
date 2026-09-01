from types import SimpleNamespace

import pytest

from _meme_manager_adapter import MemeManagerAdapter


class _Event:
    def __init__(self, text="哈哈成功了", group="1059231626"):
        self.message_str = text
        self.group_id = group
        self.message_id = "message-1"
        self.message_obj = SimpleNamespace(group=group, message_id="message-1")
        self._extras = {}

    def get_extra(self, key):
        return self._extras.get(key)

    def plain_result(self, text):
        return ("plain", text)

    def chain_result(self, chain):
        return ("chain", chain)


class _Manager:
    category_mapping = {"happy": "开心庆祝", "sad": "难过安慰"}

    def __init__(self):
        self.sent = 0

    async def compat_prepare_message(self, event, message):
        assert "&&happy&&" in message
        return {
            "cleaned_chain": SimpleNamespace(chain=["cleaned"]),
            "images": ["image"],
            "temp_files": [],
        }

    async def compat_send_prepared_message(
            self, event, prepared, *, send_text, send_images):
        assert send_text is False and send_images is True
        self.sent += 1
        return {"sent_images_count": 1}


class _Context:
    def __init__(self, manager):
        self.manager = manager

    def get_registered_star(self, name):
        assert name == "meme_manager"
        return SimpleNamespace(star_cls=self.manager)


@pytest.mark.asyncio
async def test_disabled_bridge_returns_plain_text(monkeypatch):
    monkeypatch.setenv("DUDUDA_MEME_MANAGER", "0")
    adapter = MemeManagerAdapter(_Context(_Manager()))
    result = await adapter.prepare_result(_Event(), "好耶，成功了。")
    assert result == ("plain", "好耶，成功了。")


@pytest.mark.asyncio
async def test_allowed_group_prepares_and_flushes_one_image(monkeypatch):
    monkeypatch.setenv("DUDUDA_MEME_MANAGER", "1")
    monkeypatch.setenv("DUDUDA_MEME_MANAGER_GROUPS", "1059231626,481757927")
    monkeypatch.setenv("DUDUDA_MEME_MANAGER_RATE", "100")
    monkeypatch.setenv("DUDUDA_MEME_MANAGER_COOLDOWN", "0")
    manager = _Manager()
    adapter = MemeManagerAdapter(_Context(manager))
    event = _Event()
    result = await adapter.prepare_result(event, "好耶，成功了。")
    assert result == ("chain", ["cleaned"])
    assert await adapter.flush_after_text(event) == 1
    assert await adapter.flush_after_text(event) == 0
    assert manager.sent == 1


@pytest.mark.asyncio
async def test_unlisted_group_and_high_risk_fail_closed(monkeypatch):
    monkeypatch.setenv("DUDUDA_MEME_MANAGER", "1")
    monkeypatch.setenv("DUDUDA_MEME_MANAGER_GROUPS", "1059231626")
    monkeypatch.setenv("DUDUDA_MEME_MANAGER_RATE", "100")
    manager = _Manager()
    adapter = MemeManagerAdapter(_Context(manager))
    assert await adapter.prepare_result(
        _Event(group="other"), "好耶，成功了。") == (
            "plain", "好耶，成功了。")
    assert await adapter.prepare_result(
        _Event(text="我想死"), "先保证安全。") == (
            "plain", "先保证安全。")
