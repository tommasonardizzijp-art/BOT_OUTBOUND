from app.adapters.ai import AIClient


class FakeAIClient:
    async def generate(self, system_prompt: str, user_prompt: str, max_tokens: int) -> str:
        return "ok"


def test_fake_ai_client_matches_protocol():
    client: AIClient = FakeAIClient()
    assert client is not None
