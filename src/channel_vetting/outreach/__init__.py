"""Approved prospect -> one sent, logged email.

`ledger` is the duplicate-send guard and the reason this package is safe to
re-run; `templates` renders, `mailer` is the Gmail transport and the last
point a real address can be swapped for the demo mailbox, and `sender` is
the CLI entry point that drives all three.
"""
