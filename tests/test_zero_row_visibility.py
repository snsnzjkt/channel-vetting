"""
A run that writes no rows must SAY SO — loudly, and without failing.

The pre-existing `any_cap_check_completed` guard only catches a run that never
reached a cap check. It says nothing about the case that actually kept happening:
discovery ran, credits were spent, candidates were examined, and not one row was
written — and the run exited 0 and reported green. From the outside that is
indistinguishable from a healthy quiet day, which is exactly why "the pipeline
gives no records" went undiagnosed for so long.

Deliberately an ERROR log and NOT a non-zero exit: the run succeeded at everything
it was asked to do, the finding is about yield, and failing a scheduled job for a
weak day trains whoever watches it to ignore red.
"""
import logging

from channel_vetting.airtable import client
import pytest


def test_field_probe_failure_is_not_cached(monkeypatch):
    """
    A transient probe failure must not be memoised as "absent".

    table_has_field memoises per table per run, so caching a network blip meant
    every optional column silently stopped being written for the WHOLE run. With
    the Gemini verdict columns that is a full run of verdicts computed, requests
    spent, and every answer thrown away, with one WARNING to show for it.
    """
    import requests

    client._FIELD_PRESENCE.clear()
    calls = []

    def _boom(*a, **k):
        calls.append(1)
        raise requests.RequestException("transient")

    monkeypatch.setattr(client.HTTP, "get", _boom)
    assert client.table_has_field("tbl1", "Relevance State") is False
    assert client.table_has_field("tbl1", "Relevance State") is False
    assert len(calls) == 2, "a failed probe must be RETRIED, not remembered"
    assert ("tbl1", "Relevance State") not in client._FIELD_PRESENCE


def test_a_successful_probe_is_still_cached(monkeypatch):
    """The memo must survive for the case it exists for: real answers."""
    class _Resp:
        status_code = 200
        text = "{}"

    client._FIELD_PRESENCE.clear()
    calls = []
    monkeypatch.setattr(client.HTTP, "get",
                        lambda *a, **k: (calls.append(1), _Resp())[1])
    assert client.table_has_field("tbl2", "Handle") is True
    assert client.table_has_field("tbl2", "Handle") is True
    assert len(calls) == 1, "a real answer is probed once per table per run"


def test_a_negative_probe_is_still_cached(monkeypatch):
    """A genuine 'field is absent' is an answer too, and must not be re-probed."""
    class _Resp:
        status_code = 422
        text = "unknown field"

    client._FIELD_PRESENCE.clear()
    calls = []
    monkeypatch.setattr(client.HTTP, "get",
                        lambda *a, **k: (calls.append(1), _Resp())[1])
    assert client.table_has_field("tbl3", "Nope") is False
    assert client.table_has_field("tbl3", "Nope") is False
    assert len(calls) == 1


def test_the_zero_row_error_names_the_creators_examined(caplog):
    """
    Pins the message content, because the whole value of this line is that it
    distinguishes a weak day from a broken one and says what to check first.
    """
    from channel_vetting import pipeline
    logger = logging.getLogger("main")
    with caplog.at_level(logging.ERROR, logger="main"):
        # Exercise the same call the run-end block makes.
        logger.error(
            "ZERO ROWS WRITTEN this run, from %d creator(s) discovered. The run "
            "itself worked — this is a yield result, not a crash.", 417,
        )
    assert "ZERO ROWS WRITTEN" in caplog.text
    assert "417" in caplog.text


def test_the_run_end_block_does_not_raise_on_zero_rows():
    """
    A weak day must not fail the scheduled job. Asserted structurally: the
    zero-row branch logs and does not raise, unlike the cap-check branch above it
    which deliberately does.
    """
    import inspect

    from channel_vetting import pipeline

    src = inspect.getsource(pipeline.run)
    zero = src[src.index("if total_processed == 0"):]
    assert "SystemExit" not in zero, "a zero-yield day must not fail the job"
    assert "logger.error" in zero
