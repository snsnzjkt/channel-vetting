"""Spend guards, one per metered upstream.

Each tracker persists its own counter to a JSON ledger under `data/` so the
limit survives the run, and each refuses the call rather than overspending:
YouTube quota units, influencers.club credits (real money), and Gemini
free-tier requests.
"""
