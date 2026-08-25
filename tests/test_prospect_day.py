"""The prospect day must be timezone-pinned, not host-local."""
import re


def test_today_iso_format():
    from channel_vetting.core.prospect_day import today_iso

    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", today_iso())


def test_uses_configured_zone_not_host_local(monkeypatch):
    """A UTC CI runner, a UTC+8 laptop, and Toronto must agree on the day."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from channel_vetting.core import prospect_day

    # 2026-08-08 02:00 UTC is still 2026-08-07 (22:00) in Toronto, and is
    # already 2026-08-08 (10:00) on the UTC+8 dev machine. Only the
    # configured zone may decide.
    fixed = datetime(2026, 8, 8, 2, 0, tzinfo=ZoneInfo("UTC"))

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz)

    monkeypatch.setattr(prospect_day, "datetime", FrozenDatetime)
    monkeypatch.setattr(prospect_day, "PROSPECT_DAY_TZ", "America/Toronto")
    assert prospect_day.today_iso() == "2026-08-07"
