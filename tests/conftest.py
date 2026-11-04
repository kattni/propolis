import httpx2
import pytest

from propolis.core.github import GitHub


@pytest.fixture
def anyio_backend():
    return "asyncio"


class FakeGitHub:
    """Serves canned JSON by method and path, and records every request."""

    def __init__(self):
        self.routes = {("GET", "/user"): {"login": "kattni"}}
        self.requests = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        key = (request.method, request.url.path)
        self.requests.append(key)
        if key not in self.routes:
            return httpx2.Response(404, json={"message": "Not Found"})
        return httpx2.Response(200, json=self.routes[key])


@pytest.fixture
def fake_github():
    return FakeGitHub()


@pytest.fixture
async def github(fake_github):
    async with GitHub("token", transport=httpx2.MockTransport(fake_github)) as github:
        yield github
