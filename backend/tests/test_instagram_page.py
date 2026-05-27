import asyncio

from app.browser.instagram_page import InstagramPage


class _FakeLocator:
    def __init__(self, count: int, visible: bool = True) -> None:
        self._count = count
        self._visible = visible

    @property
    def first(self):
        return self

    async def count(self) -> int:
        return self._count

    async def is_visible(self) -> bool:
        return self._visible


class _FakePage:
    def __init__(self, *, url: str, visible_selectors: set[str] | None = None) -> None:
        self.url = url
        self._visible_selectors = visible_selectors or set()

    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator(1 if selector in self._visible_selectors else 0)


def test_stories_viewer_detects_story_url():
    page = _FakePage(url="https://www.instagram.com/stories/example/123/")

    assert asyncio.run(InstagramPage(None)._is_stories_viewer_open(page))


def test_stories_viewer_detects_reply_input_overlay():
    page = _FakePage(
        url="https://www.instagram.com/example/",
        visible_selectors={'input[placeholder*="Reply"]'},
    )

    assert asyncio.run(InstagramPage(None)._is_stories_viewer_open(page))


def test_stories_viewer_ignores_normal_profile():
    page = _FakePage(url="https://www.instagram.com/example/")

    assert not asyncio.run(InstagramPage(None)._is_stories_viewer_open(page))
