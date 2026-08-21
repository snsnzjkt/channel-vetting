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

The second autouse fixture isolates the CREDIT LEDGER for the same reason,
one layer over. `credit_tracker` persists real money spent to
`credit_log.json` in the working directory, and without redirection a test
run appends its fixture spend to the production ledger — which then eats
the real daily/monthly headroom and starts REFUSING live lookups. That is
not hypothetical: the first run of the credit-guard suite wrote 10.14
credits into the repo's own ledger and the next run of the same suite
failed, because every test was correctly told the budget was exhausted.
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


@pytest.fixture(autouse=True)
def isolate_credit_ledger(tmp_path, monkeypatch):
    """
    Point the credit ledger at a per-test temp file, and lift the spend ceilings
    out of the way.

    Patches the names bound in `credit_tracker`, not `config`: the module does
    `from config import ...`, so those values are copied into its own globals at
    import and patching config afterwards has no effect.

    Two halves, both load-bearing:

    - **The temp file.** Without it a test run appends its fixture spend to the
      production ledger, eating real headroom until live lookups are refused.
      Not hypothetical: the first run of the credit suite wrote 10.14 credits
      into the repo's own ledger, and the next run of the same suite failed
      because every test was correctly told the budget was exhausted.
    - **The lifted ceilings.** The production defaults (10/day) would otherwise
      apply suite-wide, silently truncating any discovery test that pages more
      than 20 times at 0.5 credits — and the symptom would present as a
      pagination bug in an unrelated module. This autouse fixture must not change
      behaviour by default; its sibling `block_real_http` only ever *fails* by
      default. A test that wants a real ceiling opts in via `credit_ceilings`.
    """
    import credit_tracker

    monkeypatch.setattr(
        credit_tracker, "CREDIT_LOG_FILE", str(tmp_path / "credit_log.json")
    )
    monkeypatch.setattr(credit_tracker, "INFLUENCERS_MAX_CREDITS_PER_DAY", float("inf"))
    monkeypatch.setattr(credit_tracker, "INFLUENCERS_MAX_CREDITS_PER_MONTH", float("inf"))


@pytest.fixture(autouse=True)
def isolate_rejected_handles(tmp_path, monkeypatch):
    """
    Point the rejected-handle cache at a per-test temp file.

    Exactly the same hazard as `isolate_credit_ledger` above, and it had already
    happened by the time this fixture was written: 58 synthetic handles
    (`a0`..`a57`, all stamped 2026-08-20) were found sitting in the repo's real
    `rejected_handles.json`, in the Home Theater niche, alongside 262 genuine
    ones. A test suite had written to production state.

    Why only that file was hit: `tests/test_rejected_handles.py` monkeypatches
    `REJECTED_HANDLES_FILE` itself, so it was always clean. Every OTHER test that
    reaches a code path calling `rejected_handles.add()` — the discovery-wiring
    tests especially — had nothing pointing it away from the real file.

    Patches the name bound in `rejected_handles`, not `config`, for the reason
    the sibling fixture spells out: the module does `from config import ...`, so
    the value is copied into its globals at import.

    The contamination was not harmless. Those handles are sent to the vendor in
    `exclude_handles` on every run, so a polluted cache spends part of a
    10,000-handle request budget on strings that match no creator.
    """
    import rejected_handles

    monkeypatch.setattr(
        rejected_handles, "REJECTED_HANDLES_FILE", str(tmp_path / "rejected_handles.json")
    )


@pytest.fixture(autouse=True)
def isolate_run_metrics(tmp_path, monkeypatch):
    """
    Point the per-run metrics log at a per-test temp file.

    Third instance of the same hazard, after the credit ledger and the reject
    cache — and this one was self-inflicted: the tests that exercise run()
    (test_pipeline_regressions, test_discovery_window) call the real
    run_metrics.write, so the very first suite run after the feature landed
    appended ~30 junk records to the repo's real run_metrics.jsonl, most of them
    a fixture called "Test Niche" with every counter at zero.

    That is worse here than noise. The file exists to answer "did this change
    help", and its readers average across runs — so fixture records with zero
    rows and zero credits drag every before/after comparison toward zero. A
    polluted metrics file does not look broken, it looks like bad results.
    """
    import run_metrics

    monkeypatch.setattr(
        run_metrics, "RUN_METRICS_FILE", str(tmp_path / "run_metrics.jsonl")
    )


@pytest.fixture
def credit_ceilings(monkeypatch):
    """
    Opt into real spend ceilings: `credit_ceilings(day=1.0, month=5.0)`.

    One factory rather than a near-identical local fixture per test file, so the
    numbers are visible in the test that depends on them.
    """
    import credit_tracker

    def _set(day=1.0, month=5.0):
        monkeypatch.setattr(credit_tracker, "INFLUENCERS_MAX_CREDITS_PER_DAY", day)
        monkeypatch.setattr(credit_tracker, "INFLUENCERS_MAX_CREDITS_PER_MONTH", month)

    return _set


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "allow_http: permit real HTTP transport in this test (see block_real_http).",
    )
