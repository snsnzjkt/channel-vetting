"""
Shared pytest fixtures.

The autouse guard below blocks any real HTTP request from a test. It was
added alongside the move from bare `requests.get()` to the shared sessions
in `http_client.py`: the tests mock HTTP by monkeypatching a *specific*
attribute (e.g. `monkeypatch.setattr(airtable_client.HTTP, "get", ...)`),
so a call site that gets missed during a refactor — or a new one added
later without a matching mock — would quietly fall through to the real
network. With a populated `.env` on the machine running pytest, that means
spending real YouTube quota and, worse, writing real rows to the
production Airtable base from a test run.

Blocking at `HTTPAdapter.send` rather than at the socket layer is
deliberate: it is the single chokepoint every `requests` call passes
through regardless of which session issued it, and it leaves the error
message able to name the offending method and URL.
"""
import pytest
from requests.adapters import HTTPAdapter


@pytest.fixture(autouse=True)
def block_real_http(request, monkeypatch):
    """
    Fail any test that reaches the real network.

    Opt out with @pytest.mark.allow_http for a test that deliberately
    exercises transport behaviour against a local stub.
    """
    if request.node.get_closest_marker("allow_http"):
        return

    def _blocked(self, prepared_request, **kwargs):
        raise RuntimeError(
            "This test tried to make a REAL HTTP request: "
            f"{prepared_request.method} {prepared_request.url}\n"
            "Mock it (e.g. monkeypatch.setattr(<module>.HTTP, 'get', ...)) or "
            "mark the test @pytest.mark.allow_http if that is intended."
        )

    monkeypatch.setattr(HTTPAdapter, "send", _blocked)


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "allow_http: permit real HTTP transport in this test (see block_real_http).",
    )
