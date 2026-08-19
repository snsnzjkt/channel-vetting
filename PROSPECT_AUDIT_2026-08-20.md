# Prospect audit — rows that fail the 2026-08-20 criteria

Read-only run of `audit_prospects.py`. **Nothing was written or deleted.**

- 146 rows re-checked against the live YouTube API
- 43 fail some current rule; **27 fail specifically because of today's change**
- the other 16 already failed rules that predate today (view floors, Shorts, cadence)


## Home Theater — 8 rows

| Channel | Airtable Status | Why it now fails |
|---|---|---|
| Garrett Odom | Rejected | `no_declared_country` — no country set on the channel |
| Marco Zamora | Rejected | `no_declared_country` — no country set on the channel |
| ADAM Audio | Rejected | `outside_search_zone` — declared country DE |
| Chan's Tech Review | Rejected | `outside_search_zone` — description says CN |
| Joseph K Morris | Approved | `outside_search_zone` — declared country NO |
| Sander Recommends | Approved | `outside_search_zone` — declared country CH |
| Sean's World | Approved | `outside_search_zone` — declared country FR |
| The Artmann | Rejected | `outside_search_zone` — declared country DE |

## Lifestyle Sofa — 19 rows

| Channel | Airtable Status | Why it now fails |
|---|---|---|
| Entertainment Tonight | New | `broadcast_tv` — broadcast_tv_name |
| Escape To The Country | New | `broadcast_tv` — broadcast_tv_phrase |
| HGTV | New | `broadcast_tv` — broadcast_tv_name |
| Christy Cleans | New | `no_declared_country` — no country set on the channel |
| JUS KAYSHA | New | `no_declared_country` — no country set on the channel |
| Maggi Fuchs | New | `no_declared_country` — no country set on the channel |
| Mansa Plus | New | `no_declared_country` — no country set on the channel |
| kim and tanaka | New | `no_declared_country` — no country set on the channel |
| A new life in central France | New | `outside_search_zone` — declared country FR |
| Daichi🇯🇵 | New | `outside_search_zone` — title flies the JP flag |
| Her 86m2 | New | `outside_search_zone` — declared country DE |
| Inside Japan Living | New | `outside_search_zone` — title names JP |
| Interior Insights | New | `outside_search_zone` — declared country DE |
| Joyce Hellenah. | New | `outside_search_zone` — description says KE |
| LIV KENYA | Rejected | `outside_search_zone` — title names KE |
| Linet_ke | Rejected | `outside_search_zone` — description says KE |
| Olesya & house | New | `outside_search_zone` — description says BY |
| Tanita Giu | New | `outside_search_zone` — declared country UA |
| Thai Girl Gift & Foreigner Joe | New | `outside_search_zone` — description says TH |

## Everything else that fails (pre-existing rules, not today's change)

| Channel | Niche | Status | Rule |
|---|---|---|---|
| Adrianne MG | Lifestyle Sofa | Approved | `below_view_minimum` |
| DuchessLifestyle | Lifestyle Sofa | Approved | `below_view_minimum` |
| Tay BeepBoop | Lifestyle Sofa | Approved | `below_view_minimum` |
| Charlotte's Channel | Lifestyle Sofa | Rejected | `excluded_topic` |
| See Technology | Home Theater | Rejected | `shorts_only` |
| ShayNicoleXO | Lifestyle Sofa | Rejected | `shorts_only` |
| Sona Gasparian | Lifestyle Sofa | Rejected | `shorts_only` |
| Camille - Offbeat Look | Lifestyle Sofa | Rejected | `too_few_longform_videos` |
| Kayli King | Lifestyle Sofa | Rejected | `too_few_longform_videos` |
| Karin Bohn | Lifestyle Sofa | Rejected | `upload_cadence_too_low` |
| DanKamYouKnow | Home Theater | Approved | `video_below_view_minimum` |
| Dantier and Balogh Design Studio | Home Theater | Rejected | `video_below_view_minimum` |
| Jason Witmer | Home Theater | Approved | `video_below_view_minimum` |
| Jasper Tran - House Design Ideas | Home Theater | Rejected | `video_below_view_minimum` |
| Lorenzo Centioni | Home Theater | Rejected | `video_below_view_minimum` |
| Tishanae’s Diary | Lifestyle Sofa | Rejected | `video_below_view_minimum` |

## Counts by reason

- `outside_search_zone`: 17 **(new today)**
- `no_declared_country`: 7 **(new today)**
- `video_below_view_minimum`: 6
- `shorts_only`: 3
- `broadcast_tv`: 3 **(new today)**
- `below_view_minimum`: 3
- `too_few_longform_videos`: 2
- `excluded_topic`: 1
- `upload_cadence_too_low`: 1
