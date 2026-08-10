"""
cleanup_external_duplicates.py is the only script in this repo that
PERMANENTLY deletes Airtable rows, and there is no undo on our end. These
tests pin its three guards (see that module's docstring):

1. no delete_record() call without --confirm;
2. a read failure aborts instead of deleting from a table it could only
   partially read;
3. a mass-delete circuit breaker that needs --yes-delete-many, in the same
   shape as audit_blocklist.py's --yes-create-status-option.

Every test wires delete_record to pytest.fail unless deleting is the
behaviour under test, so a regression shows up as a failing test rather
than as a passing one that happened not to check.
"""
import sys

import pytest


class _Resp:
    def __init__(self, status_code, payload=None, text="error body"):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def _prospects_page(n):
    """One page of `n` prospect rows, handles chan0..chan{n-1}."""
    return _Resp(200, {
        "records": [
            {"id": f"rec{i}", "fields": {"Channel ID": f"UC{i}", "Channel Name": f"Chan {i}"}}
            for i in range(n)
        ]
    })


def _setup(monkeypatch, argv, *, rows=4, matching=("chan0",), response=None):
    """Wire up a run with `rows` prospects, of which `matching` handles are
    present in the external index. Returns the list deletes land in."""
    import cleanup_external_duplicates as cleanup

    monkeypatch.setattr(cleanup.time, "sleep", lambda s: None)
    monkeypatch.setattr(
        cleanup, "fetch_external_handles",
        lambda force_refresh=False: {h: "Home Theatre – YouTube Leads" for h in matching},
    )
    monkeypatch.setattr(
        cleanup.HTTP, "get", lambda *a, **k: response or _prospects_page(rows)
    )
    # Channel ID "UC3" -> handle "chan3", so the external index above
    # decides which rows match.
    monkeypatch.setattr(
        cleanup, "get_channel_stats",
        lambda channel_id: {"handle": f"chan{channel_id.removeprefix('UC')}"},
    )
    # Both niche tables are configured from .env, which may be empty in a
    # test environment — pin them so the loop actually runs.
    monkeypatch.setattr(cleanup, "TABLES", {"Home Theater": "Channel Prospects"})
    monkeypatch.setattr(sys, "argv", ["cleanup_external_duplicates.py", *argv])

    deleted: list[tuple[str, str]] = []
    monkeypatch.setattr(
        cleanup, "delete_record",
        lambda table_name, record_id: (deleted.append((table_name, record_id)), True)[1],
    )
    return cleanup, deleted


def test_dry_run_deletes_nothing(monkeypatch):
    """The headline guard: without --confirm, main() must return before the
    delete loop even though it found a real match to report."""
    cleanup, _ = _setup(monkeypatch, argv=[])
    monkeypatch.setattr(
        cleanup, "delete_record",
        lambda *a, **k: pytest.fail("deleted a row without --confirm"),
    )

    cleanup.main()


def test_confirm_deletes_only_the_matched_rows(monkeypatch):
    """The other half of the contract: --confirm deletes exactly the rows
    that were printed, and nothing else in the table."""
    cleanup, deleted = _setup(monkeypatch, argv=["--confirm"], rows=4, matching=("chan0", "chan2"))

    cleanup.main()

    assert deleted == [("Channel Prospects", "rec0"), ("Channel Prospects", "rec2")]


def test_mass_delete_aborts_without_the_opt_in(monkeypatch):
    """All 4 of 4 rows matching is far more likely to be a broken handle
    index than a table that is genuinely all duplicates, so it must refuse
    rather than empty the table unattended."""
    cleanup, _ = _setup(
        monkeypatch, argv=["--confirm"], rows=4, matching=("chan0", "chan1", "chan2", "chan3"),
    )
    monkeypatch.setattr(
        cleanup, "delete_record",
        lambda *a, **k: pytest.fail("tripped the mass-delete guard and deleted anyway"),
    )

    with pytest.raises(SystemExit):
        cleanup.main()


def test_mass_delete_proceeds_with_the_explicit_opt_in(monkeypatch):
    cleanup, deleted = _setup(
        monkeypatch,
        argv=["--confirm", "--yes-delete-many"],
        rows=4,
        matching=("chan0", "chan1", "chan2", "chan3"),
    )

    cleanup.main()

    assert len(deleted) == 4


def test_a_normal_sized_match_set_does_not_trip_the_guard(monkeypatch):
    """The guard must not be so eager that the script's actual purpose
    needs an override flag every time — 1 of 4 rows is a routine cleanup."""
    cleanup, deleted = _setup(monkeypatch, argv=["--confirm"], rows=4, matching=("chan1",))

    cleanup.main()

    assert deleted == [("Channel Prospects", "rec1")]


def test_read_failure_aborts_without_deleting(monkeypatch):
    """A non-200 used to end pagination silently (an error body has no
    "records" key), so the script would delete based on however many pages
    happened to succeed — a list no human ever reviewed."""
    cleanup, _ = _setup(monkeypatch, argv=["--confirm"], response=_Resp(500))
    monkeypatch.setattr(
        cleanup, "delete_record",
        lambda *a, **k: pytest.fail("deleted from a table that could not be read"),
    )

    with pytest.raises(SystemExit):
        cleanup.main()


def test_read_failure_error_body_is_truncated(monkeypatch):
    """The abort message goes to a human's terminal — safe_body() keeps a
    20k-char Airtable error from burying the reason."""
    cleanup, _ = _setup(
        monkeypatch, argv=["--confirm"], response=_Resp(500, text="z" * 20_000),
    )

    with pytest.raises(cleanup.ProspectFetchError) as excinfo:
        cleanup._fetch_all_prospects("Channel Prospects")

    assert "z" * 20_000 not in str(excinfo.value)
    assert "truncated" in str(excinfo.value)
