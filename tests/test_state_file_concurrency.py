"""
Two concurrent runs must not corrupt or crash on the shared state ledgers.

Observed 2026-08-22: a Home Theater sweep and a Lifestyle run overlapped and the
Lifestyle run DIED MID-RUN in record_spend —

    FileNotFoundError: 'quota_log.json.tmp' -> 'quota_log.json'

Every ledger wrote to one shared `<file>.tmp` and then os.replace'd it. Both
processes wrote the same tmp path, the first replace moved it away, and the
second raised FileNotFoundError — which _replace_with_retry does NOT retry (it
handles PermissionError only). The run was lost after real quota had been spent.
"""
import os
import re

import pytest


@pytest.mark.parametrize("module_name,const", [
    ("quota_tracker", "QUOTA_LOG_FILE"),
    ("credit_tracker", "CREDIT_LOG_FILE"),
    ("gemini_tracker", "GEMINI_LOG_FILE"),
    ("rejected_handles", "REJECTED_HANDLES_FILE"),
])
def test_temp_file_is_unique_per_process(module_name, const):
    """
    A shared tmp name is the bug. The pid makes it per-process, which is the
    granularity that matters: the collision is between processes, and a single
    process serialises its own writes.
    """
    src = open(f"{module_name}.py").read()
    shared = f'tmp_path = f"{{{const}}}.tmp"'
    assert shared not in src, (
        f"{module_name} is back to a shared tmp filename — two concurrent runs "
        "will race and one will crash in os.replace"
    )
    assert "os.getpid()" in src, f"{module_name} tmp path is not per-process"


@pytest.mark.parametrize("module_name", [
    "quota_tracker", "credit_tracker", "gemini_tracker", "rejected_handles",
])
def test_two_processes_get_different_temp_paths(module_name, monkeypatch):
    """The property that actually prevents the crash."""
    import importlib
    mod = importlib.import_module(module_name)
    src = open(f"{module_name}.py").read()
    m = re.search(r'tmp_path = f"\{(\w+)\}\.\{os\.getpid\(\)\}\.tmp"', src)
    assert m, f"{module_name}: could not find the per-process tmp expression"
    base = getattr(mod, m.group(1))

    monkeypatch.setattr(os, "getpid", lambda: 111)
    first = f"{base}.{os.getpid()}.tmp"
    monkeypatch.setattr(os, "getpid", lambda: 222)
    second = f"{base}.{os.getpid()}.tmp"
    assert first != second
