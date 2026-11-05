from datetime import datetime

import httpx2
import pytest

from propolis.core.github import GitHub


@pytest.fixture
def anyio_backend():
    return "asyncio"


class FakeGitHub:
    """Serves canned JSON by method and path, and records every request.

    A route can also be a function that takes the request and returns JSON.
    """

    def __init__(self):
        self.routes = {("GET", "/user"): {"login": "kattni"}}
        self.requests = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        key = (request.method, request.url.path)
        self.requests.append(key)
        if key not in self.routes:
            return httpx2.Response(404, json={"message": "Not Found"})
        body = self.routes[key]
        if callable(body):
            body = body(request)
        if since := request.url.params.get("since"):
            # Like GitHub, list only notifications updated since then.
            since = datetime.fromisoformat(since)
            body = [
                item
                for item in body
                if datetime.fromisoformat(item["updated_at"]) > since
            ]
        return httpx2.Response(200, json=body)


@pytest.fixture
def fake_github():
    return FakeGitHub()


@pytest.fixture
async def github(fake_github):
    async with GitHub("token", transport=httpx2.MockTransport(fake_github)) as github:
        yield github
