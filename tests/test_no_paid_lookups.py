"""The paid email-lookup integrations are gone and stay gone."""
import importlib

import pytest


@pytest.mark.parametrize("module_name", ["hunter_client", "modash_client", "modash_backfill"])
def test_paid_modules_are_removed(module_name):
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module_name)


def test_config_has_no_paid_keys():
    import config

    for attr in ("HUNTER_API_KEY", "MODASH_API_KEY", "MODASH_API_BASE_URL"):
        assert not hasattr(config, attr), f"{attr} should be removed from config"


def test_resolve_email_has_no_hunter_param():
    import inspect

    import main

    assert "use_hunter" not in inspect.signature(main.resolve_email).parameters


def test_email_blocklist_still_excludes_freemail():
    """Removing Hunter deletes DOMAIN_SEARCH_BLOCKLIST, never EMAIL_DOMAIN_BLOCKLIST."""
    import enrichment

    assert "gmail.com" in enrichment.FREEMAIL_DOMAINS
    assert "gmail.com" not in enrichment.EMAIL_DOMAIN_BLOCKLIST
    assert not enrichment.is_blocklisted_email_domain("gmail.com")
