from app.adapters.ai import AIClient
from app.adapters.browser import DMBrowser


class FakeAIClient:
    async def generate(self, system_prompt: str, user_prompt: str, max_tokens: int) -> str:
        return "Ciao, messaggio test."


class FakeDMBrowser:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    async def open(self):
        return self

    async def ensure_logged_in(self, account_id: str) -> None:
        return None

    async def browse_feed(self, duration_seconds: float) -> None:
        return None

    async def send_dm(self, username: str, message: str, pre_send_callback=None) -> None:
        if pre_send_callback is not None:
            ok = await pre_send_callback()
            if not ok:
                raise RuntimeError("pre-send rejected")
        self.sent.append((username, message))

    async def close(self) -> None:
        return None


def test_fake_adapters_cover_dm_contract():
    ai: AIClient = FakeAIClient()
    browser: DMBrowser = FakeDMBrowser()

    assert ai is not None
    assert browser is not None
