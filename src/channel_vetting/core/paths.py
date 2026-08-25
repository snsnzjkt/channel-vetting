"""
Where machine-local runtime state lives: one directory, `data/`.

Every ledger and cache in this project used to default to a BARE FILENAME,
which resolved against the current working directory — so the whole repo root
doubled as the state directory, and `git status` on a working tree was a wall
of untracked JSON. Worse, the resolution was silent: run the pipeline from a
different directory and it started from an empty quota log and an empty credit
ledger, both of which fail OPEN on a missing file, so the spend guards were one
`cd` away from being disabled.

Both problems are the same fix — name the directory once, here.

The paths are still RELATIVE (to `data/`, not to the repo), because that is
what CI and cron already rely on: both run from the repo root. Override
CHANNEL_VETTING_DATA_DIR to point somewhere else, e.g. a persistent volume.
Per-file env overrides (CREDIT_LOG_FILE, GEMINI_CACHE_FILE, ...) still win
over this, and the test fixtures redirect through those.

Nothing here imports from this project: `config` itself calls data_path(), so
this has to sit below it.
"""
import os
from pathlib import Path

DATA_DIR = Path(os.getenv("CHANNEL_VETTING_DATA_DIR", "data"))


def data_path(name: str) -> str:
    """
    `name` resolved inside DATA_DIR, as a str.

    Creates DATA_DIR if it is missing. That is not a convenience — the ledgers
    write atomically (`os.replace` of `<file>.<pid>.tmp` onto `<file>`), and
    into a directory that does not exist that raises FileNotFoundError AFTER
    the metered call has already been made and the money already spent. A fresh
    clone, or a custom CHANNEL_VETTING_DATA_DIR, would hit exactly that.

    str rather than Path because the callers concatenate: the per-process tmp
    name is built as an f-string on top of the value.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return str(DATA_DIR / name)
