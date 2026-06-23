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
        "issuer_premium":  5,
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


def issuer_spread_at_tenor(tenor, issuer_bonds, edge_buffer=2.0):
    """Linear interpolation between adjacent issuer bonds. For tenors just
    outside the range (within `edge_buffer` years) we clamp to the nearest
    edge bond's spread — defensive cap, not free extrapolation. Returns
    None if tenor is too far from any issuer bond.
    """
    if not issuer_bonds:
        return None
    s = sorted(issuer_bonds, key=lambda b: b["ytm"])
    lo, hi = s[0]["ytm"], s[-1]["ytm"]
    # Too far outside the observed range → no floor
    if tenor < lo - edge_buffer or tenor > hi + edge_buffer:
        return None
    # Just past the edges → clamp to the nearest edge bond
    if tenor < lo:
        return float(s[0]["spread"])
    if tenor > hi:
        return float(s[-1]["spread"])
    # Inside the range → linear interp
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


# ── Pricing chain ──────────────────────────────────────────────────

def compute_pricing(payload, scenario):
    """Build per-tenor pricing rows + the inputs used to build them.

    payload  = whatever build_payload() returns (issuer / treasuries / peers).
    scenario = dict from default_scenario().

    Nelson-Siegel is fit to peers + the issuer's OWN outstanding bonds
    (when available). The issuer's own long-dated paper anchors the long
    end of the curve, preventing the NS extrapolation pathology where the
    indicative 30Y can come out tighter than the issuer's existing 30Y
    bonds. Credit adjustment is still computed from rating-vs-peer-avg
    only — the curve fit handles the issuer-specific level, the credit
    adj handles the rating differential.
    """
    treasuries = payload["treasuries"]
    peers = payload["peers"]
    issuer_bonds = payload.get("issuerBonds") or []
    issuer_rating = payload["issuer"].get("rating", "NR")

    # Anchor the curve with issuer's own bonds where available
    fit_bonds = list(peers) + list(issuer_bonds)
    beta = fit_nelson_siegel(fit_bonds)
    base_peer = {t: max(0, round(ns_predict(t, beta))) for t in TENORS}
    # Floor at the issuer's observed secondary curve where the issuer has
    # bonds. The new issue can't price tighter than where the issuer's
    # existing paper currently trades — investors would just buy the
    # outstanding bonds instead. Linear interpolation between adjacent
    # issuer bonds; no extrapolation beyond the issuer's bond range.
    floor_applied = {}
    for t in TENORS:
        obs = issuer_spread_at_tenor(t, issuer_bonds)
        if obs is not None and obs > base_peer[t]:
            floor_applied[t] = (base_peer[t], round(obs))
            base_peer[t] = round(obs)
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

    rows = []
    for t in TENORS:
        tsy = (treasuries.get(str(t)) or 0.0) + scenario["rate_shock"] / 100.0
        peer = base_peer[t] + scenario["spread_shock"]
        final_s = peer + credit_adj[t] + issuer_adj[t] + eff_nic
        yld = tsy + final_s / 100.0
        rows.append({
            "tenor":         t,
            "label":         TENOR_LABEL[t],
            "treasury":      round(tsy, 3),
            "peer":          peer,
            "credit":        credit_adj[t],
            "issuer":        issuer_adj[t],
            "nic":           eff_nic,
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
