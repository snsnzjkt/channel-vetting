"""
Channel vetting: discover YouTube creators, qualify them, and cold-email the
approved ones.

The pipeline runs in the order the sub-packages are listed here, and each
stage is gated by the one before it — nothing reaches `outreach` that
`verification` has not passed.

    discovery     find candidate channels (YouTube search, influencers.club)
    enrichment    real stats and a contact address for each candidate
    verification  is this channel actually about the niche? (Gemini)
    ranking       order the reviewer's queue best-first
    airtable      the system of record: prospects, suppression, send state
    outreach      render and send, once, to an Approved prospect
    budget        spend guards for every metered upstream
    core          leaf utilities with no project dependencies

`pipeline` orchestrates the first five for a whole niche; `outreach.sender`
is the separate entry point for sending.
"""
