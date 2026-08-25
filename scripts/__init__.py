"""
Operational scripts: one-off audits, backfills, and measurement probes.

Not part of the pipeline — nothing under `channel_vetting` imports from here,
and the dependency only ever points the other way. They are a package rather
than loose files because several carry SAFETY guards worth testing (report-only
must not write to Airtable; a measurement probe must not bypass the credit
ledger), and a test can only import what is importable.

Run them from the repo root, e.g. `python scripts/audit/audit_blocklist.py`.
"""
