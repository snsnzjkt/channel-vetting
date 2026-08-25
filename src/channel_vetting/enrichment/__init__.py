"""Turning a channel ID into a decidable, contactable prospect.

`channels` fetches the real stats. The email chain then runs in order and
stops at the first validated address: `email_influencers` (paid, by handle),
then `email_browser` (free, follows the channel's own link list).
`external_dedupe` drops channels already tracked elsewhere in the base.
"""
