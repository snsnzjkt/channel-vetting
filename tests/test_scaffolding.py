"""Confirms the test harness collects and that project modules import."""


def test_harness_runs():
    assert True


def test_config_imports():
    import config

    assert hasattr(config, "QUOTA_CEILING")
