"""Which Hardcover errors are worth trying again later."""

import pytest

from bookbot import hardcover
from bookbot.hardcover import HardcoverClient, HardcoverError, HardcoverUnavailable


class FakeResponse:
    def __init__(self, status, payload):
        self.status, self._payload = status, payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._payload

    async def text(self):
        return str(self._payload)


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)

    def post(self, url, **kwargs):
        return FakeResponse(*self.responses.pop(0))


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    async def sleep(seconds):
        pass

    monkeypatch.setattr(hardcover.asyncio, "sleep", sleep)


async def search(*responses):
    return await HardcoverClient(FakeSession(*responses), "token").search("Crossroads of Ravens")


async def test_search_without_results_means_search_is_down():
    # As Hardcover answered while its website said search was temporarily unavailable.
    with pytest.raises(HardcoverUnavailable, match="no results object"):
        await search((200, {"data": {"search": {"results": None}}}))


async def test_server_errors_and_rate_limits_are_worth_trying_later():
    with pytest.raises(HardcoverUnavailable, match="HTTP 503"):
        await search((503, {}), (503, {}))
    limited = [(429, "slow down")] * (hardcover.MAX_RATE_LIMIT_WAITS + 1)
    with pytest.raises(HardcoverUnavailable):
        await search(*limited)


@pytest.mark.parametrize(
    "response",
    [(401, {"error": "invalid token"}), (200, {"errors": [{"message": "bad field"}]})],
)
async def test_errors_that_would_happen_again_are_not(response):
    with pytest.raises(HardcoverError) as raised:
        await search(response)
    assert not isinstance(raised.value, HardcoverUnavailable)


async def test_a_working_search_still_works():
    hit = {"document": {"id": "7", "title": "Crossroads of Ravens"}, "text_match_info": {}}
    [found] = await search((200, {"data": {"search": {"results": {"hits": [hit]}}}}))
    assert found.book.title == "Crossroads of Ravens" and found.tokens_dropped == 0
