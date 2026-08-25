"""The system of record.

`client` is the REST integration (dedupe checks, create/update).
`do_not_contact` enforces the suppression list. `outreach_store` is the
Airtable-backed implementation of the send ledger's storage protocols, so
`outreach.ledger` itself stays storage-agnostic.
"""
