"""Where candidates come from: YouTube search.list and influencers.club.

`niches` is the registry that drives both — what gets searched and which
Airtable table the result lands in. `search_zones` bounds the result set
geographically; `rejected_handles` keeps the vendor from being paid twice
to surface a creator this niche already turned down.
"""
