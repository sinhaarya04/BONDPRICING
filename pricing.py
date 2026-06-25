"""Bond pricing math — Nelson-Siegel curve fit + indicative pricing chain.

Port of the JavaScript pricing logic in index.html (lines ~514-820) so the
CLI (and any future server-side use) can produce the same per-tenor
indicative pricing the frontend shows. Pure functions, no I/O, no globals.

Stages:
  1. fit_nelson_siegel(peer_bonds) -> (b0, b1, b2)            closed-form OLS
  2. compute_credit_adj(rating, peers) -> {tenor: bps}        rating diff
  3. compute_issuer_adj(premium_bps)   -> {tenor: bps}        per-tenor scale
  4. effective_nic(base, deal_size_mm) -> (eff_nic, bump)     big-deal bump
  5. compute_pricing(payload, scenario)                       per-tenor rows
  6. compute_sensitivity(payload, scenario)                   10Y yield grid
  7. compute_notes(scenario, eff_nic_data)                    conditional flags
"""
import math


# ── Constants ─────────────────────────────────────────────────────

TENORS = (2, 5, 7, 10, 20, 30)
TENOR_LABEL = {2: "2Y", 5: "5Y", 7: "7Y", 10: "10Y", 20: "20Y", 30: "30Y"}
LAMBDA = 1.37  # Nelson-Siegel decay parameter (fixed; turns NS into a linear OLS)

# Issuer-curve isolated-fit threshold.
#
# When the issuer has >= MIN_ISSUER_BONDS_FOR_ISOLATED_FIT outstanding bonds,
# fit Nelson-Siegel on the issuer's OWN bonds only (peers become a display-
# only sanity check on the chart, no regression input). This is the
# Bloomberg NIA methodology: the issuer's existing curve is the dispositive
# signal for where THIS issuer can fund; peers tell you about the sector
# but they don't price the deal.
#
# Below the threshold, fall back to the combined peers+issuer regression
# (curve shape discipline matters more than peer contamination when the
# issuer's own curve is sparse).
#
# 8 is the BB-GPT-recommended floor for stable NS fits (3-parameter
# regression — 3 unknowns, 8 observations gives 5 degrees of freedom and
# enough tenor spread to constrain the level/slope/curvature factors).
# Future refinement: replace the count with a goodness-of-fit gate (e.g.
# require RMSE < 15 bps on issuer-only fit before triggering isolated mode).
MIN_ISSUER_BONDS_FOR_ISOLATED_FIT = 8

# Long-end NSS extrapolation cap (BB-GPT recommendation 2026-06-25).
#
# NSS predictions become unreliable beyond the longest observed bond
# maturity — the curve can shoot off to infinity or collapse to zero
# depending on which factor (slope/curvature) dominates the fit. For
# Humana this produced a 30Y indicative of T+255 when secondary trades
# T+125-155 (~100 bps too wide).
#
# Fix: when target tenor > longest issuer bond maturity, cap the
# predicted spread at:
#     last_observed_spread + LONG_END_EXTRAP_BPS_PER_YEAR * (tenor - last_ytm)
# This is a soft cap (min of NSS prediction and the linear extrapolation),
# so NSS still wins when it predicts tighter — only the blow-up case
# gets caught.
LONG_END_EXTRAP_BPS_PER_YEAR = 3.5  # BB-GPT recommendation: 3-4 bps/yr

# Low-coupon distorted-bond exclusion (BB-GPT recommendation 2026-06-25).
#
# Bonds with coupon far below current market rates trade at deep
# discounts. Their G-spreads are distorted by the price effect — the
# discount creates implicit yield pickup that inflates the spread vs
# the bond's "true" credit risk. Including them in the NSS fit pulls
# the short-end curve up artificially. HUM 1.35% 2027 (discount) and
# HUM 2.15% 2032 are the canonical examples.
#
# Threshold: exclude when bond coupon < LOW_COUPON_RATIO * (current 5Y
# Treasury yield, in %). At 5Y UST = 4.15% and ratio = 0.5, threshold
# is 2.075% — excludes the two HUM bonds above without false-positiving
# anything reasonable.
LOW_COUPON_RATIO = 0.65  # BB-GPT round-2 refinement: 0.5 missed HUM 3.7% / 3.125% 2029 bonds

# Iterative residual-based outlier exclusion (BB-GPT refinement
# 2026-06-25). Hybrid/subordinated/100-yr bonds sometimes appear in an
# issuer's stack and price on a different curve from senior unsecured.
# HUM cusip 444859CE0 (30.2y 6.625% at T+277) was the canonical case.
#
# Algorithm (per BB-GPT):
#   1. Fit NSS on current cohort
#   2. Compute per-bond residuals (actual_spread - NSS prediction)
#   3. Drop bonds with |residual / sigma| > sample-size-adjusted threshold
#   4. Refit on survivors; repeat until convergence (max 3 iterations)
#
# Sample-size-adjusted threshold:
#   - n >= 20 bonds: 2.5σ (NSS well-conditioned; tighter outlier net)
#   - 8 <= n < 20:   3.0σ (sparse curve; don't over-trim)
#   - n < 8:         no filter (every bond matters)
# Long-end bonds (>20y maturity) get an additional floor of 3.0σ
# because long bonds naturally disperse more (wider bid-ask, fewer
# comparables), so a 2.5σ filter would falsely flag legitimate prints.
#
# Iteration matters: a single-pass filter uses a fit that's already
# distorted by the very outliers you're trying to remove. Two or three
# refit cycles converge cleanly.
ZSCORE_MAX_ITERATIONS = 3
ZSCORE_LONG_END_TENOR = 20  # tenor (y) above which 3.0σ floor applies

# Rating-tier NIC schedule (BB-GPT round-2 refinement 2026-06-25).
#
# A single tenor-offset table applied to every issuer mispriced both
# AA+ (Apple's exceptional long-end demand → too wide) and BBB (stressed
# names → too tight). BB-GPT's recommended absolute NIC schedule per
# rating tier:
#                AA+    A      BBB
#   2Y           +1     +1     +2
#   5Y           +1     +3     +4
#   7Y           +2     +5     +6
#   10Y          +3     +6     +8
#   20Y          +5     +8     +12
#   30Y          +7     +10    +15
#
# Rationale: AA+ issuers (AAPL, MSFT) have deep loyal long-duration
# bid; minimal concession needed. A-rated (DIS) get the canonical
# mid-range. BBB (HUM) faces price-sensitive buyers and stressed-name
# premium, especially at long tenors.
#
# These are ABSOLUTE NIC bps per tenor — they replace the slider value
# entirely when a rating tier matches. The slider's old role (set the
# 10Y baseline) is preserved as an override: when the slider deviates
# from the tier's 10Y default, we shift the whole curve by the delta
# (keeps slider responsive, lets desks tighten or widen the whole
# schedule from one knob).
NIC_TIER_SCHEDULES = {
    # AA+ tier trimmed at 20Y/30Y per BB-GPT round-3 (Apple-style
    # exceptional long-end demand doesn't require +5/+7 — actual deals
    # land at +3/+4 of secondary).
    "AA+": {2: 1, 5: 1, 7: 2, 10: 3,  20: 3,  30: 4},
    "AA":  {2: 1, 5: 2, 7: 3, 10: 4,  20: 5,  30: 6},
    "AA-": {2: 1, 5: 2, 7: 4, 10: 5,  20: 6,  30: 7},
    "A+":  {2: 1, 5: 3, 7: 4, 10: 5,  20: 7,  30: 9},
    "A":   {2: 1, 5: 3, 7: 5, 10: 6,  20: 8,  30: 10},
    "A-":  {2: 2, 5: 4, 7: 5, 10: 7,  20: 9,  30: 11},
    "BBB+":{2: 2, 5: 4, 7: 6, 10: 7,  20: 10, 30: 13},
    "BBB": {2: 2, 5: 4, 7: 6, 10: 8,  20: 12, 30: 15},
    "BBB-":{2: 3, 5: 5, 7: 7, 10: 9,  20: 13, 30: 17},
}
# Fallback when rating not in table — use A-tier (median IG)
NIC_TIER_DEFAULT = {2: 1, 5: 3, 7: 5, 10: 6, 20: 8, 30: 10}


def _nic_schedule_for_rating(rating):
    """Look up per-tenor NIC schedule for a rating. Returns the table
    entry or NIC_TIER_DEFAULT if rating is unrecognized."""
    if not rating:
        return NIC_TIER_DEFAULT
    return NIC_TIER_SCHEDULES.get(rating.strip().upper(), NIC_TIER_DEFAULT)


def _is_true_callable(bond):
    """Distinguish make-whole callables (functionally bullets) from true
    callables with material optionality (BB-GPT round-3 rule):

      Primary: maturity - NXT_CALL_DT < 1.0 year => bullet (make-whole)
               maturity - NXT_CALL_DT >= 1.0 year => true callable

      Override: if no NXT_CALL_DT but CALLABLE='Y' assume true callable
                (conservative — keeps the bond out of the floor pool).

    Returns True iff the bond has material call optionality and should
    be excluded from the floor pool. Returns False for bullets and
    make-whole callables (safe to use as floor anchors).
    """
    if not bond.get("callable"):
        return False  # not callable at all -> definitely bullet
    nxt_call = bond.get("nxt_call_dt") or ""
    maturity = bond.get("maturity_dt") or ""
    if not nxt_call or not maturity:
        return True   # callable flag set but missing dates -> conservative
    try:
        from datetime import date as _date
        call_d = _date.fromisoformat(nxt_call[:10])
        mat_d  = _date.fromisoformat(maturity[:10])
        gap_years = (mat_d - call_d).days / 365.25
        # < 1 year between first call and maturity = make-whole = bullet
        return gap_years >= 1.0
    except (ValueError, TypeError):
        return True   # parse failure -> conservative


def _sample_size_threshold(n):
    """Per BB-GPT: tighter Z-filter when cohort is well-populated,
    looser when sparse. Returns None when n is too small to filter."""
    if n >= 20:
        return 2.5
    if n >= 8:
        return 3.0
    return None  # too few bonds to meaningfully outlier-filter


def _iterative_residual_outlier_removal(bonds):
    """Drop NSS-residual outliers iteratively. Per BB-GPT 2026-06-25:
    a single-pass filter uses a fit already biased by the outliers it's
    trying to remove — iterate 2-3 times until convergence.

    Returns the surviving bond list. Empty input -> empty output.
    """
    current = list(bonds)
    for _ in range(ZSCORE_MAX_ITERATIONS):
        n = len(current)
        thresh = _sample_size_threshold(n)
        if thresh is None:
            return current
        beta = fit_nelson_siegel(current)
        residuals = [b["spread"] - ns_predict(b["ytm"], beta) for b in current]
        mean_r = sum(residuals) / n
        var_r = sum((r - mean_r) ** 2 for r in residuals) / max(n - 1, 1)
        std_r = var_r ** 0.5
        if std_r == 0:
            return current
        survivors = []
        for b, r in zip(current, residuals):
            # Long bonds (>20y) naturally have wider dispersion — apply
            # at least the 3.0σ floor regardless of cohort threshold.
            local_thresh = (max(thresh, 3.0)
                            if (b.get("ytm") or 0) > ZSCORE_LONG_END_TENOR
                            else thresh)
            z = abs(r - mean_r) / std_r
            if z <= local_thresh:
                survivors.append(b)
        if len(survivors) == n:
            return current  # converged — no more bonds to drop
        if len(survivors) < 5:
            return current  # would over-trim; bail
        current = survivors
    return current


def _drop_distorted_low_coupon_bonds(bonds, current_short_yield_pct):
    """Filter out bonds with coupons far below current market rates.

    A bond trading at deep discount (coupon way below YTM) has a
    G-spread inflated by the discount pull-to-par effect. Including
    those in the NSS fit pulls the curve up artificially. BB-GPT
    flagged this on the HUM run 2026-06-25 — HUM 1.35% 2027 and
    HUM 2.15% 2032 were distorting the short/mid-end fit.

    Args:
      bonds: list of bond dicts with 'coupon' field (in %)
      current_short_yield_pct: current 5Y or 2Y UST yield (decimal pct,
        e.g. 4.15 for 4.15%) — used as the "market rate" reference
    """
    if not bonds or not current_short_yield_pct:
        return list(bonds)
    threshold = LOW_COUPON_RATIO * current_short_yield_pct
    return [b for b in bonds
            if (b.get("coupon") or 999) >= threshold]


def _cap_long_end_extrapolation(base_spreads, issuer_bonds):
    """Apply linear-extrapolation cap beyond the longest issuer bond.

    Returns a copy of base_spreads with each tenor t > longest_ytm
    capped at the linear extrapolation from the last observed bond.
    Tenors within the observed range are passed through unchanged.

    BB-GPT recommendation 2026-06-25: this alone fixed HUM 30Y from
    T+249 to ~T+150 — the smoking gun of NSS extrapolation pathology.
    """
    if not issuer_bonds:
        return dict(base_spreads), {}
    longest = max(issuer_bonds, key=lambda b: b.get("ytm") or 0)
    last_ytm = longest.get("ytm") or 0
    last_spread = longest.get("spread") or 0
    capped = {}
    cap_applied = {}
    for t, s in base_spreads.items():
        if t > last_ytm:
            cap = last_spread + LONG_END_EXTRAP_BPS_PER_YEAR * (t - last_ytm)
            new_s = max(0, round(min(s, cap)))
            if new_s < s:
                cap_applied[t] = (s, new_s)
            capped[t] = new_s
        else:
            capped[t] = s
    return capped, cap_applied

# S&P-equivalent rating scale: lower number = stronger credit.
RATING_SCALE = {
    "AAA": 1,  "AA+": 2,  "AA": 3,  "AA-": 4,
    "A+": 5,   "A": 6,    "A-": 7,
    "BBB+": 8, "BBB": 9,  "BBB-": 10,
    "BB+": 11, "BB": 12,  "BB-": 13,
    "B+": 14,  "B": 15,   "B-": 16,
    "CCC+": 17, "CCC": 18, "NR": 10,
}

# Mirrors index.html PRESETS for parity with the frontend buttons.
PRESETS = {
    "base":   {"spread_shock": 0,   "rate_shock": 0,   "nic": 5},
    "stress": {"spread_shock": 75,  "rate_shock": -25, "nic": 10},
    "rally":  {"spread_shock": -20, "rate_shock": 25,  "nic": 3},
}


def default_scenario(**overrides):
    s = {
        "spread_shock":    0,
        "rate_shock":      0,
        "nic":             5,
        "issuer_premium":  0,  # manual slider removed; only Tigress AI sets this
        "deal_size":       1000,  # $M
    }
    s.update(overrides)
    return s


# ── Nelson-Siegel ──────────────────────────────────────────────────

def _ns_factors(tau):
    r = tau / LAMBDA
    e = math.exp(-r)
    f1 = (1 - e) / r
    f2 = f1 - e
    return f1, f2


def fit_nelson_siegel(bonds):
    """Closed-form OLS fit. bonds = [{'ytm':float, 'spread':float}, ...].

    Returns (beta0, beta1, beta2). On a singular X'X (e.g. all bonds at the
    same tenor) falls back to (average spread, 0, 0) — the flat-curve
    degenerate case.
    """
    n = len(bonds)
    if n == 0:
        return (0.0, 0.0, 0.0)

    # Build the 3x3 X'X and 3x1 X'y in one pass.
    xtx = [[0.0]*3 for _ in range(3)]
    xty = [0.0]*3
    for b in bonds:
        f1, f2 = _ns_factors(b["ytm"])
        x = (1.0, f1, f2)
        y = float(b["spread"])
        for j in range(3):
            xty[j] += x[j] * y
            for k in range(3):
                xtx[j][k] += x[j] * x[k]

    # 3x3 cofactor inverse.
    a = xtx
    det = (a[0][0] * (a[1][1]*a[2][2] - a[1][2]*a[2][1])
         - a[0][1] * (a[1][0]*a[2][2] - a[1][2]*a[2][0])
         + a[0][2] * (a[1][0]*a[2][1] - a[1][1]*a[2][0]))
    if abs(det) < 1e-10:
        avg = sum(b["spread"] for b in bonds) / n
        return (avg, 0.0, 0.0)

    inv = [
        [(a[1][1]*a[2][2] - a[1][2]*a[2][1])/det,
         (a[0][2]*a[2][1] - a[0][1]*a[2][2])/det,
         (a[0][1]*a[1][2] - a[0][2]*a[1][1])/det],
        [(a[1][2]*a[2][0] - a[1][0]*a[2][2])/det,
         (a[0][0]*a[2][2] - a[0][2]*a[2][0])/det,
         (a[0][2]*a[1][0] - a[0][0]*a[1][2])/det],
        [(a[1][0]*a[2][1] - a[1][1]*a[2][0])/det,
         (a[0][1]*a[2][0] - a[0][0]*a[2][1])/det,
         (a[0][0]*a[1][1] - a[0][1]*a[1][0])/det],
    ]
    beta = [sum(inv[j][k] * xty[k] for k in range(3)) for j in range(3)]
    return tuple(beta)


def ns_predict(tau, beta):
    f1, f2 = _ns_factors(tau)
    return beta[0] + beta[1]*f1 + beta[2]*f2


def issuer_spread_at_tenor(tenor, issuer_bonds, edge_buffer=2.0, window=2.0):
    """Floor anchor at target tenor.

    BB-GPT round-2 refinement 2026-06-25: switched from linear interp
    between adjacent bonds to MEDIAN of bonds within ±`window` years
    of the target tenor. Reason: linear interp + ratchet-up-to-max
    behavior ratcheted HUM 10Y/30Y to the widest-trading callable in
    the bucket. Median is robust to those outliers.

    Fallback: if no bond is within the window, falls back to the
    old linear interp / clamp-to-edge behavior (defensive only).
    Returns None if there's no bond within `edge_buffer` of the
    target tenor at all.
    """
    if not issuer_bonds:
        return None
    in_window = [b for b in issuer_bonds
                 if abs((b.get("ytm") or 0) - tenor) <= window]
    if in_window:
        spreads = sorted(float(b["spread"]) for b in in_window)
        # True median: average of middle two for even-length lists
        n = len(spreads)
        if n % 2 == 1:
            return spreads[n // 2]
        return 0.5 * (spreads[n // 2 - 1] + spreads[n // 2])
    # No bonds in the ±window → fall back to interp-or-clamp (legacy)
    s = sorted(issuer_bonds, key=lambda b: b["ytm"])
    lo, hi = s[0]["ytm"], s[-1]["ytm"]
    if tenor < lo - edge_buffer or tenor > hi + edge_buffer:
        return None
    if tenor < lo:
        return float(s[0]["spread"])
    if tenor > hi:
        return float(s[-1]["spread"])
    for i in range(len(s) - 1):
        a, b = s[i], s[i + 1]
        if a["ytm"] <= tenor <= b["ytm"]:
            if b["ytm"] == a["ytm"]:
                return float(a["spread"])
            frac = (tenor - a["ytm"]) / (b["ytm"] - a["ytm"])
            return a["spread"] + frac * (b["spread"] - a["spread"])
    return None


# ── Adjustments ────────────────────────────────────────────────────

def _rating_to_num(r):
    return RATING_SCALE.get((r or "NR").upper(), 10)


def compute_credit_adj(issuer_rating, peers):
    """Rating differential vs peer average × per-tenor bps multiplier.

    Multiplier grows with tenor: 3 bps/notch at 2Y, 8 bps/notch at 30Y —
    captures the term-premium for credit risk.
    """
    if not peers:
        return {t: 0 for t in TENORS}
    avg_peer = sum(_rating_to_num(b["rating"]) for b in peers) / len(peers)
    issuer_num = _rating_to_num(issuer_rating)
    diff = issuer_num - avg_peer  # positive = issuer weaker
    return {t: round(diff * (3 + (t / 30) * 5)) for t in TENORS}


def compute_issuer_adj(premium_bps):
    """Issuer premium × per-tenor scale: 0.57x at 2Y → 1.5x at 30Y."""
    return {t: round(premium_bps * (0.5 + (t / 30) * 1.0)) for t in TENORS}


def effective_nic(base_nic, deal_size_mm):
    """Returns (effective_nic, bump). Big deals pay an execution premium."""
    bump = 3 if deal_size_mm > 2000 else (1 if deal_size_mm > 1500 else 0)
    return base_nic + bump, bump


def statistical_nic(bonds, beta):
    """Data-driven NIC suggestion. Returns dict with method + value.

    Primary signal: median of Bloomberg's `BB_NEW_ISSUE_SPREAD_ANALYSIS`
    (NIA <GO> methodology) across cohort bonds where the field is
    populated. This is the actual NIC paid at issue for each comparable
    bond, so the median is empirical concession for this credit space.

    Fallback: 75th percentile of absolute residuals from the NSS curve
    (dispersion heuristic) — used when none of the bonds carry the
    Bloomberg NIC field.

    Always clamped to [3, 12] bps so outliers can't blow the suggestion
    out of typical IG range. Returns None when no data is usable.
    """
    if not bonds:
        return None

    # Primary: Bloomberg's NIA-derived NIC where available
    nics = [b["nic_at_issue"] for b in bonds
            if b.get("nic_at_issue") is not None]
    if nics:
        nics_sorted = sorted(nics)
        median = nics_sorted[len(nics_sorted) // 2]
        return {
            "value":  max(3, min(12, round(median))),
            "method": "BB_NEW_ISSUE_SPREAD_ANALYSIS",
            "n":      len(nics),
            "raw":    round(median, 1),
        }

    # Fallback: residual dispersion (75th percentile of |residual|)
    residuals = [abs(b["spread"] - ns_predict(b["ytm"], beta)) for b in bonds]
    if not residuals:
        return None
    residuals.sort()
    idx = min(len(residuals) - 1, int(0.75 * len(residuals)))
    p75 = residuals[idx]
    return {
        "value":  max(3, min(12, round(p75))),
        "method": "residual_dispersion_p75",
        "n":      len(residuals),
        "raw":    round(p75, 1),
    }


# ── On-the-run anchor (wave-6, BB-GPT round-6, verified 2026-06-25) ──
# The NSS curve smooths through recent on-the-run primary-market bonds.
# This biases the indicative wide of the most liquid, most observable
# point on the curve — exactly where being wrong is worst on a pitch.
# Live data: DIS 4% 03/14/31 (issued 2026-02-12) trades at OAS+34.6
# today, but our NSS fit predicts T+52 at 5Y (Disney 39-bond curve gets
# pulled wide by the long end). Wave-6 overrides base_peer[t] at any
# tenor where an on-the-run bond exists, using the bond's actual OAS
# directly. NIC at anchored tenors is reduced to the tier's 2Y value
# (minimum concession) since investors have a live secondary reference.

# Window parameters (wave-7a spec):
ON_THE_RUN_MAX_AGE_MONTHS = 24       # RECENT vintage cutoff (≤24 months)
ON_THE_RUN_TENOR_BAND     = 1.5      # outer bucket: |ytm - target| < 1.5
ON_THE_RUN_MIN_YTM        = 0.5      # exclude bonds maturing <6mo out
ON_THE_RUN_MIN_AMT        = 500e6    # $500M liquidity floor — rejects
                                     # illiquid legacy bonds whose
                                     # G-spreads are distorted (e.g. DIS
                                     # 7.28% 2028 at $194M outstanding)


def _parse_iso_date(s):
    """Tolerant ISO-8601 date parser. Returns date or None."""
    if not s:
        return None
    try:
        from datetime import date as _date
        return _date.fromisoformat(str(s)[:10])
    except (ValueError, TypeError):
        return None


def find_on_the_run_anchor(issuer_bonds, target_tenor, today=None):
    """Select the best anchor for `target_tenor` from `issuer_bonds`.

    Selection rule (wave-7a, BB-GPT empirically-derived):
      Filters (any failure → bond rejected):
        1. ytm >= ON_THE_RUN_MIN_YTM (no <6mo bonds — terminal pull-to-par)
        2. |ytm - target_tenor| < ON_THE_RUN_TENOR_BAND (outer bucket)
        3. amt_outstanding >= ON_THE_RUN_MIN_AMT (liquidity floor)
        4. usable spread (OAS for bullets, G-spread for callables)

      Sort (within filtered candidates):
        Primary key:   vintage — RECENT (issued ≤ ON_THE_RUN_MAX_AGE_MONTHS)
                       beats LEGACY regardless of proximity. A new bond
                       priced today is the market's most authoritative
                       reference even if the closest-by-maturity bond
                       is a 10-year-old legacy issue.
        Secondary key: proximity to target_tenor (closer wins among
                       same vintage).
        Tertiary key:  issue date (most recent breaks ties).

    Why this beats a single proximity gate: the AAPL 10Y case has the
    RECENT bond (4 3/4 2035, ytm=8.88, dist=1.12) and a legacy
    alternative (4 1/2 2036, ytm=9.67, dist=0.34). Any proximity gate
    tight enough to be useful elsewhere (≤0.75) would reject the
    RECENT 10Y, and any gate loose enough to accept it (≥1.12) is too
    loose to discriminate. Vintage-first sort lets us keep a wide
    outer bucket while still picking the right anchor everywhere.

    Returns the bond dict on hit, None on miss.
    """
    if not issuer_bonds:
        return None
    from datetime import date as _date
    today = today or _date.today()
    recent_cutoff_days = ON_THE_RUN_MAX_AGE_MONTHS * 30
    candidates = []
    for b in issuer_bonds:
        ytm = b.get("ytm")
        if ytm is None:
            continue
        ytm = float(ytm)
        if ytm < ON_THE_RUN_MIN_YTM:
            continue
        if abs(ytm - float(target_tenor)) >= ON_THE_RUN_TENOR_BAND:
            continue
        if b.get("spread") is None:
            continue
        amt = b.get("amt_outstanding")
        if amt is None or float(amt) < ON_THE_RUN_MIN_AMT:
            continue
        # Callable bonds need G-spread to anchor a new bullet (OAS on a
        # callable can diverge materially from G — AAPL 4 3/4 05/12/35
        # prints OAS+64 vs G+28). Skip if G-spread isn't available.
        if b.get("callable") and b.get("g_spread") is None:
            continue
        issue_d = _parse_iso_date(b.get("issue_dt"))
        if issue_d is None:
            continue
        age_days = (today - issue_d).days
        if age_days < 0:
            continue  # future-dated issue_dt is data error
        is_recent = age_days <= recent_cutoff_days
        ytm_distance = abs(ytm - float(target_tenor))
        # Sort key tuple: (0 for RECENT / 1 for legacy, distance asc,
        # -issue_d_ordinal for most-recent tiebreak).
        candidates.append((
            0 if is_recent else 1,
            ytm_distance,
            -issue_d.toordinal(),
            b,
        ))
    if not candidates:
        return None
    candidates.sort(key=lambda x: (x[0], x[1], x[2]))
    return candidates[0][3]


def on_the_run_anchor_spread(bond):
    """Pick the right spread value from an on-the-run anchor bond for
    pricing a NEW AT-PAR BULLET. For bullet bonds OAS ≈ G-spread, so we
    use OAS_SPREAD_BID (already in bond["spread"]). For callable bonds
    we must use G-spread directly because OAS strips option premium
    relative to a different reference curve, producing a value that
    isn't the right anchor for a new at-par bullet."""
    if bond.get("callable") and bond.get("g_spread") is not None:
        return float(bond["g_spread"])
    return float(bond["spread"])


# ── Pricing chain ──────────────────────────────────────────────────

def compute_pricing(payload, scenario):
    """Build per-tenor pricing rows + the inputs used to build them.

    payload  = whatever build_payload() returns (issuer / treasuries / peers).
    scenario = dict from default_scenario().

    Nelson-Siegel curve fit follows the Bloomberg NIA methodology
    (validated 2026-06-25 against BB-GPT for the Disney miscoding case):

    ISOLATED FIT MODE — when len(issuer_bonds) >= MIN_ISSUER_BONDS_FOR_
    ISOLATED_FIT (default 8), fit NS on the issuer's OWN bonds only. The
    rating signal is embedded in those prices, so credit_adj is zeroed
    out per-tenor to avoid double-counting. Peers stay in the response
    payload for the frontend to render as overlay-only (display sanity
    check, not regression input).

    COMBINED FIT MODE — when issuer has too few bonds for a stable
    isolated fit, fall back to fitting on peers + issuer bonds together.
    The credit adjustment (rating vs peer-average) re-engages because
    the base curve now reflects a mixed-credit cohort.

    NOTE: credit_adj has different meanings in the two paths:
      - combined fit:  rating-notch adjustment vs peer-average rating
                       (because base curve is peer-contaminated)
      - isolated fit:  zero (rating is already in the issuer-only curve)
    The return value `fit_mode` tells the frontend which path was used.
    """
    treasuries = payload["treasuries"]
    peers = payload["peers"]
    issuer_bonds = payload.get("issuerBonds") or []
    issuer_rating = payload["issuer"].get("rating", "NR")

    # Branch on issuer bond count to pick the fit methodology
    use_isolated_fit = (
        len(issuer_bonds) >= MIN_ISSUER_BONDS_FOR_ISOLATED_FIT
    )
    if use_isolated_fit:
        # Bloomberg NIA path: issuer's own curve drives the fit; peers
        # are display-only on the frontend chart.
        raw_fit_bonds = list(issuer_bonds)
        fit_mode = "isolated"
    else:
        # Sparse-issuer fallback: combine peers + issuer to maintain
        # curve-shape stability when issuer has too few own bonds.
        raw_fit_bonds = list(peers) + list(issuer_bonds)
        fit_mode = "combined"
    # Drop low-coupon distorted bonds before fitting (BB-GPT rec #2).
    # Z-score alone misses these — they sit within sigma of the fit
    # but their G-spreads are economically misleading.
    short_yield_pct = float(treasuries.get("5") or treasuries.get("2") or 0.0)
    after_coupon = _drop_distorted_low_coupon_bonds(raw_fit_bonds, short_yield_pct)
    # Iterative residual-based outlier removal — catches hybrids,
    # subordinated bonds, century notes that ride a different curve
    # from senior unsecured (HUM cusip 444859CE0 was the canonical
    # case at z=3.7, pulling 30Y indicative T+100 wide).
    fit_bonds = _iterative_residual_outlier_removal(after_coupon)
    bonds_dropped = len(raw_fit_bonds) - len(fit_bonds)

    beta = fit_nelson_siegel(fit_bonds)
    raw_base_peer = {t: max(0, round(ns_predict(t, beta))) for t in TENORS}
    # Cap long-end extrapolation beyond the longest FILTERED bond
    # (BB-GPT rec #1). Pass fit_bonds (post-outlier-removal) so the cap
    # uses the longest reliable bond, not an outlier hybrid.
    base_peer, long_end_capped = _cap_long_end_extrapolation(
        raw_base_peer, fit_bonds
    )
    # Floor at the issuer's observed secondary curve where the issuer has
    # bonds. The new issue can't price tighter than where the issuer's
    # existing senior paper currently trades — investors would just buy
    # the outstanding bonds instead.
    #
    # Use the FILTERED issuer bonds (no hybrids/outliers) — the floor
    # concept is "where does the senior unsecured curve trade", not "is
    # there any junior bond trading wide that we should match". With
    # the unfiltered list, HUM cusip 444859CE0 (junior, T+277 at 30.2y)
    # would floor the 30Y new-issue indicative to T+260 even though
    # senior 30Y trades T+130-160.
    isolated_filtered = [b for b in fit_bonds
                         if (b.get("ticker") or "").upper()
                            == (payload["issuer"].get("ticker") or "").upper()]
    # Drop bonds from the floor pool that can't anchor a clean new bullet
    # issue:
    #   (a) true callables (maturity - NXT_CALL_DT >= 1y) excluded — option
    #       premium distorts spread; make-whole callables (<1y gap) kept
    #       because they're functionally bullets (round-3)
    #   (b) puttables / sinkables always excluded
    #   (c) deep-discount bonds (PX_BID < 85) excluded — pull-to-par
    #       effect compresses spread vs new par issue (round-3)
    # Note: the round-4 OAS-G > 20 bps backstop was retired in round-5.6.
    # G_SPRD_BID is not a valid Bloomberg field, and the actual OAS-G on
    # Horseshoe-style hybrids is near zero (~1 bp), so the backstop never
    # discriminated. Token-only Gate-2 in server.py handles hybrid exclusion.
    def bullet_only(bs):
        kept = []
        for b in bs:
            mty = (b.get("maturity_type") or "")
            if "PUT" in mty or "SINK" in mty:
                continue
            if _is_true_callable(b):
                continue
            px = b.get("px_bid")
            if px is not None and px < 85.0:
                continue
            kept.append(b)
        return kept
    floor_bond_pool = bullet_only(isolated_filtered) if isolated_filtered else bullet_only(fit_bonds)
    # Final fallback: if filter removed everything (rare — issuer has
    # no bullet bonds), revert to filtered fit_bonds to avoid no-floor.
    if not floor_bond_pool:
        floor_bond_pool = isolated_filtered or fit_bonds
    floor_applied = {}
    for t in TENORS:
        obs = issuer_spread_at_tenor(t, floor_bond_pool)
        if obs is not None and obs > base_peer[t]:
            floor_applied[t] = (base_peer[t], round(obs))
            base_peer[t] = round(obs)
    # On-the-run anchor override (wave-6): for any target tenor where the
    # issuer has a recently-issued bond near that maturity, use the bond's
    # actual OAS directly instead of the NSS prediction. The on-the-run
    # bond is the market's most authoritative price at that tenor. Source
    # data is the FILTERED isolated_filtered list (no hybrids).
    on_the_run_applied = {}
    for t in TENORS:
        anchor = find_on_the_run_anchor(isolated_filtered, t)
        if anchor is not None:
            old = base_peer[t]
            anchor_spread = on_the_run_anchor_spread(anchor)
            base_peer[t] = round(anchor_spread)
            on_the_run_applied[t] = {
                "cusip":    anchor.get("cusip"),
                "coupon":   anchor.get("coupon"),
                "maturity": anchor.get("maturity_dt"),
                "ytm":      anchor.get("ytm"),
                "issue_dt": anchor.get("issue_dt"),
                "callable": bool(anchor.get("callable")),
                "anchor_spread_type": (
                    "G-spread" if anchor.get("callable") and anchor.get("g_spread") is not None
                    else "OAS"
                ),
                "old":      old,
                "new":      base_peer[t],
            }
    # Credit adjustment: zero in isolated fit (rating embedded in issuer
    # curve); peer-vs-rating differential in combined fit.
    if use_isolated_fit:
        credit_adj = {t: 0 for t in TENORS}
    else:
        credit_adj = compute_credit_adj(issuer_rating, peers)
    # Data-driven NIC suggestion from curve-residual dispersion. Analyst
    # can still override via the scenario['nic'] slider; this is a
    # reference value the UI can surface.
    nic_suggested = statistical_nic(fit_bonds, beta)
    # Also fit peer-only NSS so the UI can overlay it as a comparison curve
    peer_only_beta = fit_nelson_siegel(peers) if peers else (0.0, 0.0, 0.0)
    base_peer_only = {t: max(0, round(ns_predict(t, peer_only_beta))) for t in TENORS}
    issuer_adj = compute_issuer_adj(scenario["issuer_premium"])
    eff_nic, nic_bump = effective_nic(scenario["nic"], scenario["deal_size"])

    # Rating-tier NIC schedule (BB-GPT round-2). Look up the issuer's
    # rating → tenor schedule of absolute NIC bps. The slider value
    # acts as a SHIFT vs the tier's 10Y default: when slider equals the
    # tier's 10Y NIC, no shift. Slider above/below shifts entire schedule.
    nic_tier = _nic_schedule_for_rating(issuer_rating)
    nic_shift = eff_nic - nic_tier.get(10, 6)
    # On-the-run anchored tenors get the tier's MINIMUM NIC (2Y schedule
    # value) instead of the tier's per-tenor value. BB-GPT round-6: when
    # investors have a live secondary reference at the target tenor, less
    # concession is needed to clear the bond. Slider shift still applies
    # on top so the user/AI can dial it further.
    nic_floor_for_anchored = nic_tier.get(2, 1)
    rows = []
    for t in TENORS:
        tsy = (treasuries.get(str(t)) or 0.0) + scenario["rate_shock"] / 100.0
        peer = base_peer[t] + scenario["spread_shock"]
        # Tenor- and rating-scaled NIC. Floor at 0.
        if t in on_the_run_applied:
            nic_at_t = max(0, nic_floor_for_anchored + nic_shift)
        else:
            nic_at_t = max(0, nic_tier.get(t, 6) + nic_shift)
        final_s = peer + credit_adj[t] + issuer_adj[t] + nic_at_t
        yld = tsy + final_s / 100.0
        rows.append({
            "tenor":         t,
            "label":         TENOR_LABEL[t],
            "treasury":      round(tsy, 3),
            "peer":          peer,
            "credit":        credit_adj[t],
            "issuer":        issuer_adj[t],
            "nic":           nic_at_t,
            "final_spread":  final_s,
            "yield":         round(yld, 3),
            "ipt":           final_s + 12,
            "guidance":      final_s + 5,
        })
    return {
        "beta":           beta,
        "beta_peer_only": peer_only_beta,
        "base_peer":      base_peer,
        "base_peer_only": base_peer_only,
        "credit_adj":     credit_adj,
        "issuer_adj":     issuer_adj,
        "effective_nic":  eff_nic,
        "nic_bump":       nic_bump,
        "nic_suggested":  nic_suggested,
        "floor_applied":  floor_applied,  # {tenor: (raw_ns, floored)} for UI
        "long_end_capped": long_end_capped,  # {tenor: (raw_ns, capped)} for UI
        "on_the_run_applied": on_the_run_applied,  # wave-6 anchor overrides
        "bonds_dropped":  bonds_dropped,  # how many low-coupon bonds excluded
        "fit_mode":       fit_mode,       # "isolated" (issuer-only) | "combined"
        "fit_bond_count": len(fit_bonds), # for UI badge
        "rows":           rows,
    }


# ── Sensitivity ────────────────────────────────────────────────────

SENS_SPREAD_SHOCKS = (-50, -25, 0, 25, 50, 75, 100, 150)
SENS_RATE_SHOCKS   = (-100, -50, -25, 0, 25, 50, 100)


def compute_sensitivity(payload, scenario):
    """10Y all-in yield across a (spread_shock × rate_shock) grid."""
    treasuries = payload["treasuries"]
    # Use the SAME base_peer/credit/issuer/nic as the main pricing — the
    # grid varies only spread_shock and rate_shock around them.
    p = compute_pricing(payload, {**scenario, "spread_shock": 0, "rate_shock": 0})
    base_peer_10 = p["base_peer"][10]
    credit_10 = p["credit_adj"][10]
    issuer_10 = p["issuer_adj"][10]
    nic = p["effective_nic"]
    base_tsy_10 = treasuries.get("10") or 0.0

    grid = []
    for s in SENS_SPREAD_SHOCKS:
        row = []
        for r in SENS_RATE_SHOCKS:
            tsy = base_tsy_10 + r / 100.0
            peer = base_peer_10 + s
            final_s = peer + credit_10 + issuer_10 + nic
            yld = tsy + final_s / 100.0
            row.append(round(yld, 3))
        grid.append(row)
    return {
        "spread_shocks":   list(SENS_SPREAD_SHOCKS),
        "rate_shocks":     list(SENS_RATE_SHOCKS),
        "grid":            grid,
        "current_spread":  scenario["spread_shock"],
        "current_rate":    scenario["rate_shock"],
    }


# ── Notes / flags ──────────────────────────────────────────────────

def compute_notes(scenario, eff_nic, nic_bump):
    """Conditional notes — mirrors the renderNotes() rules in index.html."""
    s = scenario["spread_shock"]
    r = scenario["rate_shock"]
    size = scenario["deal_size"]
    out = []
    if s >= 50:
        out.append(("warn", f"Spread shock of +{s} bps implies a risk-off environment. Verify with desk head before circulating."))
    if s <= -25:
        out.append(("info", f"Tight spread scenario ({s} bps). Check primary supply calendar — new issue may struggle for concession."))
    if r >= 50:
        out.append(("warn", f"Treasury shock of +{r} bps raises absolute coupon cost significantly. Consider shorter tenors."))
    if r <= -50:
        out.append(("ok",   f"Rate rally of {r} bps. Potential refinancing window — flag to origination."))
    if eff_nic > 10:
        out.append(("warn", f"Effective NIC of {eff_nic} bps is wide for IG (typical range 3-7 bps). Appropriate for stressed or debut issuers."))
    if nic_bump > 0:
        out.append(("info", f"Deal size ${size/1000:.1f}B adds +{nic_bump} bps execution risk premium (effective NIC: {eff_nic} bps)."))
    out.append(("default", "Output is indicative only. Final pricing subject to banker judgment and order book."))
    return out
