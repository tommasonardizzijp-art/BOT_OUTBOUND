import pytest

from app.services import notifier


class _Response:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


@pytest.mark.asyncio
async def test_send_telegram_falls_back_to_plain_text_when_markdown_is_rejected(monkeypatch):
    payloads: list[dict] = []

    class _Client:
        def __init__(self, timeout: float):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url: str, json: dict):
            payloads.append(json)
            if len(payloads) == 1:
                return _Response(400, "Bad Request: can't parse entities")
            return _Response(200)

    monkeypatch.setattr(notifier, "_telegram_enabled", lambda: True)
    monkeypatch.setattr(notifier.httpx, "AsyncClient", _Client)

    await notifier.send_telegram("Profili:\n  @name_with_underscore")

    assert payloads[0]["parse_mode"] == "Markdown"
    assert "parse_mode" not in payloads[1]
    assert payloads[1]["text"] == payloads[0]["text"]
