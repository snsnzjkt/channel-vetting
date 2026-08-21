"""
`InfluencerDiscovery.probe` must not spend a credit the ledger never sees.

The measurement scripts used to call `_post` directly. `_post` is only the HTTP
call — `can_afford` and `record_spend` live inside `discover()`'s page loop — so
every ablation run spent real vendor credits off the books, unchecked against
the day and month ceilings. At limit=1 that was ~0.1 credits and invisible; a
limit=20 variant sweep is ~2.0, about 20% of the day cap.

These tests pin the accounting, not the HTTP shape.
"""
import pytest

import credit_tracker
import influencer_discovery
from influencer_discovery import InfluencerDiscovery


class _Resp:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _disc(monkeypatch, payload, max_credits=10.0):
    d = InfluencerDiscovery(enabled=True, max_credits=max_credits, sleep=lambda *_: None)
    monkeypatch.setattr(d, "_post", lambda _p: _Resp(payload))
    return d


_PAYLOAD = {
    "accounts": [{"username": f"h{i}"} for i in range(5)],
    "total": 412,
    "credits_cost": 0.05,
    "credits_left": 300.0,
}


def test_probe_records_spend_in_the_ledger(monkeypatch):
    spent = []
    monkeypatch.setattr(influencer_discovery, "record_spend",
                        lambda cost, **kw: spent.append((cost, kw)) or True)
    monkeypatch.setattr(influencer_discovery, "can_afford", lambda *a, **k: True)
    monkeypatch.setattr(influencer_discovery, "record_vendor_balance", lambda *_: None)

    d = _disc(monkeypatch, _PAYLOAD)
    accounts, total = d.probe({"ai_search": "x"}, limit=5)

    assert total == 412
    assert len(accounts) == 5
    assert spent, "probe() must call record_spend — this is the whole point of it"
    cost, kw = spent[0]
    assert cost == 0.05, "the vendor's own billed cost, not an estimate"
    assert kw["kind"] == credit_tracker.KIND_DISCOVERY


def test_probe_refuses_when_the_ledger_says_no(monkeypatch):
    """A refused probe must issue no request and spend nothing."""
    monkeypatch.setattr(influencer_discovery, "can_afford", lambda *a, **k: False)
    posted = []

    d = InfluencerDiscovery(enabled=True, max_credits=10.0, sleep=lambda *_: None)
    monkeypatch.setattr(d, "_post", lambda p: posted.append(p) or _Resp(_PAYLOAD))

    accounts, total = d.probe({"ai_search": "x"}, limit=5)

    assert (accounts, total) == (None, None), "refusal must be distinguishable from no results"
    assert not posted, "a refused probe must not reach the network"


def test_probe_respects_the_per_run_ceiling(monkeypatch):
    """The per-run ceiling is projected against the probe's price, not ignored."""
    monkeypatch.setattr(influencer_discovery, "can_afford", lambda *a, **k: True)
    posted = []

    d = InfluencerDiscovery(enabled=True, max_credits=0.10, sleep=lambda *_: None)
    monkeypatch.setattr(d, "_post", lambda p: posted.append(p) or _Resp(_PAYLOAD))

    # 50 * 0.01 = 0.50, over the 0.10 ceiling.
    assert d.probe({"ai_search": "x"}, limit=50) == (None, None)
    assert not posted


def test_probe_counts_billed_creators(monkeypatch):
    """Billed count feeds the credits-per-row ratio, so a probe must not hide from it."""
    monkeypatch.setattr(influencer_discovery, "record_spend", lambda *a, **k: True)
    monkeypatch.setattr(influencer_discovery, "can_afford", lambda *a, **k: True)
    monkeypatch.setattr(influencer_discovery, "record_vendor_balance", lambda *_: None)

    d = _disc(monkeypatch, _PAYLOAD)
    d.probe({"ai_search": "x"}, limit=5)

    assert d.creators_billed == 5
    assert d.credits_spent == pytest.approx(0.05)


def test_probe_deactivates_on_a_failed_ledger_write(monkeypatch):
    """
    Same posture as discover(): if the spend cannot be persisted, stop spending.
    Continuing would authorise later requests against a total the ledger no
    longer reflects.
    """
    monkeypatch.setattr(influencer_discovery, "record_spend", lambda *a, **k: False)
    monkeypatch.setattr(influencer_discovery, "can_afford", lambda *a, **k: True)
    monkeypatch.setattr(influencer_discovery, "record_vendor_balance", lambda *_: None)

    d = _disc(monkeypatch, _PAYLOAD)
    d.probe({"ai_search": "x"}, limit=5)

    assert d.enabled is False, "a failed ledger write must stop further spend"


def test_measurement_scripts_do_not_call_post_directly():
    """
    The regression this whole module exists for. If a future edit points a
    measurement script back at _post, the spend leaves the ledger again and
    nothing else in the suite would notice.
    """
    import pathlib
    for name in ("measure_discovery_pool.py", "measure_query_union.py"):
        src = pathlib.Path(name).read_text()
        code = "\n".join(
            line for line in src.splitlines() if not line.lstrip().startswith("#")
        )
        assert "_post(" not in code, (
            f"{name} calls _post directly, which bypasses can_afford and "
            "record_spend. Use InfluencerDiscovery.probe() instead."
        )


def test_rejected_handle_cache_is_isolated_from_production():
    """
    The autouse `isolate_rejected_handles` fixture must be in force.

    58 synthetic handles (`a0`..`a57`) were found in the repo's REAL
    `rejected_handles.json` because only `test_rejected_handles.py` patched the
    path; every other test that reached `rejected_handles.add()` wrote to
    production. Those strings then shipped to the vendor in `exclude_handles`
    on every run, spending part of a 10,000-handle budget on nothing.
    """
    import rejected_handles as rh

    assert rh.REJECTED_HANDLES_FILE != "rejected_handles.json", (
        "the autouse isolation fixture is not active — a test writing a reject "
        "would land in the repo's production cache"
    )


def test_production_reject_cache_has_no_synthetic_handles():
    """Guards the cleanup itself: bare `a`+digits is not a valid YouTube handle."""
    import json
    import pathlib
    import re

    path = pathlib.Path("rejected_handles.json")
    if not path.exists():
        return
    niches = json.loads(path.read_text()).get("niches", {})
    synthetic = re.compile(r"^a\d{1,2}$")
    found = {
        niche: sorted(h for h in handles if synthetic.match(h))
        for niche, handles in niches.items()
    }
    assert not any(found.values()), f"test handles are back in production state: {found}"
