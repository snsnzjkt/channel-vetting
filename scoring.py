"""
Scoring heuristics: fake-follower risk and a weighted overall score.

All weights/thresholds are constants at the top of the file so they can
be tuned without touching the scoring logic itself.
"""
import math

# --- Fake follower risk thresholds ---
VIEW_TO_SUB_RATIO_HIGH_RISK = 0.02   # avg_views / subscriber_count below this is suspicious
ENGAGEMENT_RATE_HIGH_RISK = 0.5      # engagement rate (%) below this is suspicious
HIGH_SUB_FLOOR = 100_000             # subscriber count considered "high" for the combo red flag
COMBO_RED_FLAG_VIEW_RATIO = 0.005    # view/sub ratio below this, combined with high subs, is a major red flag

# --- Overall score weights (must sum to 1.0) ---
WEIGHT_SUBSCRIBERS = 0.20
WEIGHT_AVG_VIEWS = 0.20
WEIGHT_ENGAGEMENT_RATE = 0.20
WEIGHT_UPLOAD_CONSISTENCY = 0.15
WEIGHT_TRUST = 0.15
WEIGHT_NICHE_MATCH = 0.10

# Normalization ceilings: raw metric value that maps to a normalized 100.
# Views/subs use log scale since they span multiple orders of magnitude
# across a candidate pool (a 2M-sub channel isn't "10x better" than a
# 200K one in the same linear sense engagement rate is).
SUBSCRIBER_CEILING = 1_000_000
AVG_VIEWS_CEILING = 500_000
ENGAGEMENT_RATE_CEILING = 8.0        # % — very high engagement rate that maxes the sub-score
UPLOAD_FREQ_IDEAL_MIN = 2            # videos/month considered fully "consistent"
UPLOAD_FREQ_IDEAL_MAX = 20           # above this, treated as still fully consistent (not penalized)


def calc_fake_follower_risk(subscriber_count: int, avg_views: float, engagement_rate: float) -> float:
    """
    Heuristic 0-100 fake-follower / bought-audience risk score (higher = riskier).

    Signals:
      - Low view-to-subscriber ratio (viewers not matching claimed audience size).
      - Low engagement rate (likes+comments per view).
      - The combination of a large subscriber base with very low views,
        which is the strongest indicator of purchased/bot followers.
    """
    if subscriber_count <= 0:
        return 100.0

    view_to_sub_ratio = avg_views / subscriber_count
    risk = 0.0

    # View-to-sub ratio component (up to 45 points).
    if view_to_sub_ratio < VIEW_TO_SUB_RATIO_HIGH_RISK:
        shortfall = 1 - (view_to_sub_ratio / VIEW_TO_SUB_RATIO_HIGH_RISK)
        risk += 45 * min(1.0, max(0.0, shortfall))

    # Engagement rate component (up to 35 points).
    if engagement_rate < ENGAGEMENT_RATE_HIGH_RISK:
        shortfall = 1 - (engagement_rate / ENGAGEMENT_RATE_HIGH_RISK)
        risk += 35 * min(1.0, max(0.0, shortfall))

    # Major red flag: large subscriber count paired with near-zero views.
    if subscriber_count >= HIGH_SUB_FLOOR and view_to_sub_ratio < COMBO_RED_FLAG_VIEW_RATIO:
        risk += 20

    return round(min(100.0, risk), 1)


def _normalize_log(value: float, ceiling: float) -> float:
    """Log-scale normalize `value` against `ceiling` to a 0-100 range."""
    if value <= 0:
        return 0.0
    score = math.log10(value + 1) / math.log10(ceiling + 1) * 100
    return round(min(100.0, max(0.0, score)), 1)


def _normalize_linear(value: float, ceiling: float) -> float:
    if ceiling <= 0:
        return 0.0
    score = (value / ceiling) * 100
    return round(min(100.0, max(0.0, score)), 1)


def _normalize_upload_frequency(videos_per_month: float) -> float:
    """
    Score upload consistency: ramps up to 100 between 0 and IDEAL_MIN
    videos/month, stays at 100 through IDEAL_MAX, then eases off for
    extreme spam-posting frequencies above that.
    """
    if videos_per_month <= 0:
        return 0.0
    if videos_per_month < UPLOAD_FREQ_IDEAL_MIN:
        return round((videos_per_month / UPLOAD_FREQ_IDEAL_MIN) * 100, 1)
    if videos_per_month <= UPLOAD_FREQ_IDEAL_MAX:
        return 100.0
    # Beyond the ideal max, gently taper (still a decent score, not a cliff).
    overage_ratio = UPLOAD_FREQ_IDEAL_MAX / videos_per_month
    return round(max(50.0, overage_ratio * 100), 1)


def calc_overall_score(
    subs: int,
    avg_views: float,
    engagement_rate: float,
    upload_freq: float,
    fake_risk: float,
    niche_match: float,
) -> float:
    """
    Weighted composite 0-100 score across subscriber count, avg views,
    engagement rate, upload consistency, trust (inverse of fake risk),
    and niche match.

    `niche_match` is a 0-100 input score representing how well the
    channel's content matches the target niche (currently a manual/human
    input during review; see README for how it's expected to be supplied).
    """
    trust_score = 100 - fake_risk

    weighted = (
        WEIGHT_SUBSCRIBERS * _normalize_log(subs, SUBSCRIBER_CEILING)
        + WEIGHT_AVG_VIEWS * _normalize_log(avg_views, AVG_VIEWS_CEILING)
        + WEIGHT_ENGAGEMENT_RATE * _normalize_linear(engagement_rate, ENGAGEMENT_RATE_CEILING)
        + WEIGHT_UPLOAD_CONSISTENCY * _normalize_upload_frequency(upload_freq)
        + WEIGHT_TRUST * trust_score
        + WEIGHT_NICHE_MATCH * niche_match
    )
    return round(min(100.0, max(0.0, weighted)), 1)
