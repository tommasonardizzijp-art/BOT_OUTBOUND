from typing import Protocol


class AIClient(Protocol):
    async def generate(self, system_prompt: str, user_prompt: str, max_tokens: int) -> str: ...
