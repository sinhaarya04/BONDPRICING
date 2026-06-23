#!/usr/bin/env python3
"""Bond Pricing Engine — Bloomberg backend.

Connects to Bloomberg Terminal on localhost:8194 and serves live data.
  GET /                    -> index.html
  GET /api/load/<TICKER>   -> JSON { issuer, treasuries, peers }

Usage:
  python server.py

Requires a running Bloomberg Terminal authenticated on localhost:8194.
"""
import http.server
import json
import os
import sys
import urllib.request
import urllib.error
from datetime import date

import blpapi

import cache_store

PORT = 5050

# ── Bloomberg Session ──────────────────────────────────────────────

_session = None


def get_session():
    """Return a connected Bloomberg session (singleton, auto-reconnect)."""
    global _session
    if _session is not None:
        return _session
    opts = blpapi.SessionOptions()
    opts.setServerHost("localhost")
    opts.setServerPort(8194)
    _session = blpapi.Session(opts)
    if not _session.start():
        _session = None
        raise ConnectionError(
            "Cannot connect to Bloomberg Terminal on localhost:8194. "
            "Make sure the Terminal is running."
        )
    if not _session.openService("//blp/refdata"):
        _session.stop()
        _session = None
        raise ConnectionError("Connected but cannot open //blp/refdata service.")
    return _session


def ref_data(securities, fields):
    """Bloomberg ReferenceDataRequest -> {security: {field: value}}."""
    session = get_session()
    svc = session.getService("//blp/refdata")
    req = svc.createRequest("ReferenceDataRequest")
    for s in securities:
        req.append("securities", s)
    for f in fields:
        req.append("fields", f)
    session.sendRequest(req)

    result = {}
    while True:
        ev = session.nextEvent(10000)
        for msg in ev:
            if msg.hasElement("securityData"):
                arr = msg.getElement("securityData")
                for i in range(arr.numValues()):
                    sd = arr.getValueAsElement(i)
                    sec = sd.getElementAsString("security")
                    has_err = sd.hasElement("securityError")
                    d = {}
                    if not has_err and sd.hasElement("fieldData"):
                        fd = sd.getElement("fieldData")
                        for f in fields:
                            try:
                                el = fd.getElement(f)
                                val = el.getValue()
                                # Convert non-JSON-serializable types
                                if hasattr(val, "isoformat"):
                                    val = val.isoformat()
                                d[f] = val
                            except Exception:
                                d[f] = None
                    else:
                        for f in fields:
                            d[f] = None
                    result[sec] = d
        if ev.eventType() == blpapi.Event.RESPONSE:
            break
    return result


def fetch_bloomberg_peers(ticker):
    """Pull Bloomberg's equity peer list for an issuer.

    Bloomberg exposes peers via the bulk field BLOOMBERG_PEERS (verified via
    probe_peers.py: returns an array of ~10-20 peer tickers in
    "<TICKER> <COUNTRY>" format, e.g. "ACN US", "ATO FP", "CCC LN").
    Other commonly cited fields (EQY_PEER_TICKERS, BLOOMBERG_PEERS_OVERRIDE_TICKERS,
    RELATIVE_VALUATION_PEERS) return "Field not valid" on this seat.

    Returns the full raw list as strings (not US-filtered). Caller filters.
    """
    session = get_session()
    svc = session.getService("//blp/refdata")
    req = svc.createRequest("ReferenceDataRequest")
    req.append("securities", f"{ticker} US Equity")
    req.append("fields", "BLOOMBERG_PEERS")
    session.sendRequest(req)

    out = []
    while True:
        ev = session.nextEvent(10000)
        for msg in ev:
            if not msg.hasElement("securityData"):
                continue
            sds = msg.getElement("securityData")
            for i in range(sds.numValues()):
                sd = sds.getValue(i)
                if sd.hasElement("securityError"):
                    continue
                if not sd.hasElement("fieldData"):
                    continue
                fd = sd.getElement("fieldData")
                if not fd.hasElement("BLOOMBERG_PEERS"):
                    continue
                el = fd.getElement("BLOOMBERG_PEERS")
                # Bulk array — each row has one sub-element with the peer ticker
                for k in range(el.numValues()):
                    row = el.getValue(k)
                    if row.numElements() > 0:
                        out.append(row.getElement(0).getValueAsString())
        if ev.eventType() == blpapi.Event.RESPONSE:
            break
    return out


def suggest_peers(ticker, sector):
    """Build the suggested peer list for confirmation before pricing.

    Tries Bloomberg's BLOOMBERG_PEERS first, filters to US-listed tickers
    (drops foreign issuers that don't typically have USD bond curves), then
    falls back to the static sector mapping if Bloomberg returns nothing
    useful.

    Returns a dict with keys:
      source:    "bloomberg" | "sector" | "override"
      peers:     list of US-only equity tickers to feed bond fetch
      raw:       Bloomberg's full peer list (incl. foreign) for display
      dropped:   foreign tickers we filtered out (for display)
    """
    up = ticker.upper()

    # Hand-curated overrides win first (AMZN -> hyperscalers, etc.)
    if up in TICKER_PEER_OVERRIDE:
        peers = [t for t in TICKER_PEER_OVERRIDE[up] if t.upper() != up]
        return {"source": "override", "peers": peers, "raw": [], "dropped": []}

    raw = []
    try:
        raw = fetch_bloomberg_peers(ticker)
    except Exception as e:
        sys.stderr.write(f"  BLOOMBERG_PEERS fetch failed: {e}\n")

    if raw:
        # "ACN US" -> US-listed; "ATO FP" -> foreign. Token after space = country.
        us_peers, dropped = [], []
        for p in raw:
            parts = p.split()
            if len(parts) >= 2 and parts[1].upper() == "US":
                root = parts[0].upper()
                if root != up and root not in us_peers:
                    us_peers.append(root)
            else:
                dropped.append(p)
        if us_peers:
            return {"source": "bloomberg", "peers": us_peers, "raw": raw, "dropped": dropped}

    # Fall back to sector mapping
    sector_peers = [t for t in find_sector_peers(sector) if t.upper() != up]
    return {"source": "sector", "peers": sector_peers, "raw": raw, "dropped": []}


def find_issuer_bonds_via_openfigi(ticker, max_bonds=15):
    """Enumerate an issuer's outstanding USD corporate bonds via OpenFIGI.

    OpenFIGI is the public, free mirror of Bloomberg's symbology
    (developer.bloomberg.com explicitly endorses it for symbology lookups).
    `instrumentListRequest` on //blp/instruments returns search-box
    autocomplete hints (e.g. "UNH<corp>"), NOT queryable security IDs —
    those fail every downstream ReferenceDataRequest. OpenFIGI returns
    proper FIGIs that load directly as "<FIGI> Corp" via the Yellow Key
    syntax documented at developer.bloomberg.com/.../symbology.

    Args:
      ticker:    Equity ticker root (e.g. "UNH"). OpenFIGI matches this
                 against the issuer's ticker field on every outstanding bond.
      max_bonds: Cap on returned securities per issuer.

    Returns:
      List of Bloomberg security strings like ["BBG00XXXXXXX Corp", ...].
      Empty list on any failure (network, rate-limit, no matches) so the
      caller's per-peer loop continues with other peers.

    Env:
      OPENFIGI_API_KEY (optional). Anonymous limit is 25 req/min; with a
      free key the limit is 250 req/min. We send the X-OPENFIGI-APIKEY
      header iff the env var is set.
    """
    # /v3/search takes a SINGLE query object (not an array — that's /v3/mapping).
    # `maturity: [today, null]` filters out already-redeemed bonds at the
    # source (otherwise OpenFIGI's default sort surfaces oldest issues first,
    # most of which have matured 10–25 years ago and have no live pricing).
    today_iso = date.today().isoformat()
    body = json.dumps({
        "query": ticker,
        "marketSecDes": "Corp",
        "currency": "USD",
        "maturity": [today_iso, None],
    }).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    api_key = os.environ.get("OPENFIGI_API_KEY")
    if api_key:
        headers["X-OPENFIGI-APIKEY"] = api_key

    try:
        req = urllib.request.Request(
            "https://api.openfigi.com/v3/search",
            data=body, headers=headers, method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as e:
        sys.stderr.write(f"  OpenFIGI lookup failed for {ticker}: {e}\n")
        return []

    # /v3/search returns {"data": [{...}, ...], "next": "..."} per query.
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []

    out = []
    for item in data[:max_bonds]:
        figi = item.get("figi") if isinstance(item, dict) else None
        if figi:
            out.append(f"{figi} Corp")
    return out


def safe_float(val):
    """Convert Bloomberg field value to float, or None."""
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def years_to_maturity_from_date(maturity_iso, ref_date=None):
    """Compute current years-to-maturity from a Bloomberg MATURITY field.

    Bloomberg's `YRS_TO_MTY_TDY` field returns None on FIGI-loaded Corp
    securities (verified via direct probe), so we derive it ourselves from
    the MATURITY date string. Returns None on parse failure or matured bond.
    """
    if not maturity_iso:
        return None
    try:
        # MATURITY can come back as "2027-06-20" or "2027-06-20T00:00:00".
        d = date.fromisoformat(str(maturity_iso)[:10])
    except (ValueError, TypeError):
        return None
    today = ref_date or date.today()
    days = (d - today).days
    if days <= 0:
        return None  # already matured
    return days / 365.25


# ── Sector -> Peer Tickers ─────────────────────────────────────────

SECTOR_PEERS = {
    # Healthcare
    "Managed Health Care":
        ["UNH", "ELV", "CI", "CNC", "MOH", "HUM"],
    "Health Care Services":
        ["CVS", "MCK", "CAH", "COR", "DGX", "UNH", "ELV", "CI"],
    "Health Care Facilities":
        ["HCA", "THC", "UHS", "CYH", "SEM"],
    "Health Care Equipment":
        ["MDT", "ABT", "SYK", "BSX", "BAX", "BDX", "ZBH", "EW"],
    "Health Care Supplies":
        ["BAX", "BDX", "HOLX", "ALGN", "COO"],
    "Health Care Distributors":
        ["MCK", "CAH", "COR"],
    "Pharmaceuticals":
        ["JNJ", "PFE", "MRK", "LLY", "BMY", "ABBV", "AMGN", "GILD"],
    "Biotechnology":
        ["AMGN", "GILD", "REGN", "VRTX", "BIIB"],
    "Life Sciences Tools & Services":
        ["TMO", "DHR", "A", "IQV", "MTD"],

    # Technology
    "Systems Software":
        ["MSFT", "ORCL", "CRM", "INTU", "ADBE", "NOW"],
    "Application Software":
        ["ORCL", "CRM", "INTU", "ADBE", "SAP", "WDAY", "SNPS"],
    "Technology Hardware, Storage & Peripherals":
        ["AAPL", "HPQ", "HPE", "DELL", "WDC", "STX"],
    "Semiconductors":
        ["INTC", "TXN", "QCOM", "AVGO", "ADI", "MCHP", "NXPI"],
    "Semiconductor Materials & Equipment":
        ["AMAT", "LRCX", "KLAC", "TER"],
    "IT Consulting & Other Services":
        ["ACN", "IBM", "CTSH", "INFY", "WIT"],
    "Communications Equipment":
        ["CSCO", "MSI", "JNPR", "VIAV"],
    "Electronic Equipment & Instruments":
        ["ROK", "FTV", "KEYS", "ZBRA"],

    # Financials
    "Diversified Banks":
        ["JPM", "BAC", "WFC", "C", "USB", "PNC"],
    "Regional Banks":
        ["USB", "PNC", "TFC", "FITB", "KEY", "RF", "HBAN", "MTB", "CFG"],
    "Investment Banking & Brokerage":
        ["GS", "MS", "SCHW", "RJF", "IBKR"],
    "Property & Casualty Insurance":
        ["AIG", "TRV", "ALL", "CB", "PGR", "HIG"],
    "Life & Health Insurance":
        ["MET", "PRU", "AFL", "UNUM", "LNC", "GL"],
    "Multi-line Insurance":
        ["AIG", "HIG", "TRV", "ALL"],
    "Reinsurance":
        ["RNR", "EVRG"],
    "Consumer Finance":
        ["AXP", "COF", "DFS", "SYF", "ALLY"],
    "Asset Management & Custody Banks":
        ["BLK", "BK", "STT", "TROW", "IVZ"],
    "Financial Exchanges & Data":
        ["ICE", "CME", "NDAQ", "CBOE", "SPGI", "MCO"],

    # Consumer Staples
    "Packaged Foods & Meats":
        ["GIS", "K", "CAG", "SJM", "CPB", "HRL", "MDLZ", "HSY"],
    "Household Products":
        ["PG", "KMB", "CLX", "CL", "CHD"],
    "Soft Drinks & Non-alcoholic Beverages":
        ["KO", "PEP", "MNST", "KDP"],
    "Brewers":
        ["TAP", "BUD", "STZ"],
    "Distillers & Vintners":
        ["BF.B", "STZ", "DEO"],
    "Tobacco":
        ["PM", "MO", "BTI"],
    "Personal Care Products":
        ["EL", "COTY"],
    "Food Retail":
        ["KR", "ACI", "SFM", "WMT"],
    "Hypermarkets & Super Centers":
        ["WMT", "COST", "TGT"],

    # Consumer Discretionary
    "Restaurants":
        ["MCD", "SBUX", "YUM", "DPZ", "CMG", "QSR"],
    "Hotels, Resorts & Cruise Lines":
        ["MAR", "HLT", "H", "RCL", "CCL", "NCLH"],
    "Casinos & Gaming":
        ["LVS", "MGM", "WYNN", "CZR"],
    "Apparel, Accessories & Luxury Goods":
        ["NKE", "PVH", "RL", "TPR", "CPRI", "HBI"],
    "Home Improvement Retail":
        ["HD", "LOW"],
    "Broadline Retail":
        ["AMZN", "EBAY", "TGT", "COST", "BABA", "MELI", "PDD", "W"],
    "General Merchandise Stores":
        ["WMT", "TGT", "COST", "DG", "DLTR"],
    "Automotive Retail":
        ["AN", "LAD", "PAG", "GPI", "AZO", "ORLY"],
    "Automobile Manufacturers":
        ["F", "GM", "TM", "STLA"],
    "Auto Parts & Equipment":
        ["APTV", "BWA", "LEA", "MGA"],
    "Homebuilding":
        ["LEN", "DHI", "PHM", "NVR", "TOL", "KBH"],

    # Industrials
    "Aerospace & Defense":
        ["BA", "RTX", "LMT", "NOC", "GD", "HII", "LHX", "TDG"],
    "Industrial Conglomerates":
        ["GE", "HON", "MMM", "ITW", "EMR"],
    "Building Products":
        ["JCI", "CARR", "TT", "MAS", "FBHS"],
    "Electrical Components & Equipment":
        ["ETN", "ROK", "AME", "GNRC"],
    "Railroads":
        ["UNP", "CSX", "NSC", "CP"],
    "Trucking":
        ["ODFL", "XPO", "SAIA", "JBHT"],
    "Airlines":
        ["DAL", "UAL", "LUV", "AAL"],
    "Air Freight & Logistics":
        ["FDX", "UPS", "EXPD", "XPO"],
    "Environmental & Facilities Services":
        ["WM", "RSG", "WCN", "GFL"],
    "Industrial Machinery & Supplies & Components":
        ["CMI", "PH", "DOV", "IR", "NDSN"],
    "Research & Consulting Services":
        ["BAH", "LDOS", "SAIC"],

    # Energy
    "Integrated Oil & Gas":
        ["XOM", "CVX", "COP", "OXY", "PSX", "TTE", "SHEL", "BP"],
    "Oil & Gas Exploration & Production":
        ["COP", "EOG", "PXD", "DVN", "FANG", "MRO", "APA"],
    "Oil & Gas Refining & Marketing":
        ["PSX", "VLO", "MPC", "PBF"],
    "Oil & Gas Storage & Transportation":
        ["WMB", "KMI", "OKE", "TRGP", "ET", "EPD", "MMP"],
    "Oil & Gas Equipment & Services":
        ["SLB", "HAL", "BKR", "FTI"],

    # Utilities
    "Electric Utilities":
        ["D", "DUK", "SO", "NEE", "AEP", "EXC", "SRE", "XEL", "ED", "WEC"],
    "Multi-Utilities":
        ["D", "DUK", "SO", "SRE", "ES", "WEC", "CMS", "AEE", "NI"],
    "Gas Utilities":
        ["ATO", "NJR", "OGS", "SWX"],
    "Water Utilities":
        ["AWK", "WTRG", "CWT", "SJW"],
    "Independent Power Producers & Energy Traders":
        ["VST", "NRG", "AES"],

    # Telecom & Media
    "Integrated Telecommunication Services":
        ["T", "VZ", "TMUS"],
    "Wireless Telecommunication Services":
        ["T", "VZ", "TMUS"],
    "Cable & Satellite":
        ["CMCSA", "CHTR", "DISH"],
    "Movies & Entertainment":
        ["DIS", "NFLX", "WBD", "PARA", "LGF"],
    "Interactive Media & Services":
        ["GOOGL", "META"],
    "Publishing":
        ["NWSA", "NYT", "GCI"],
    "Advertising":
        ["OMC", "IPG", "WPP"],

    # Real Estate
    "Specialized REITs":
        ["AMT", "CCI", "EQIX", "PLD", "DLR"],
    "Retail REITs":
        ["SPG", "O", "NNN", "KIM", "REG", "FRT"],
    "Industrial REITs":
        ["PLD", "DLR", "EQIX"],
    "Office REITs":
        ["BXP", "VNO", "SLG", "KRC", "HIW"],
    "Residential REITs":
        ["EQR", "AVB", "UDR", "MAA", "CPT"],
    "Health Care REITs":
        ["WELL", "VTR", "PEAK", "OHI"],
    "Diversified REITs":
        ["VICI", "WPC"],

    # Materials
    "Diversified Metals & Mining":
        ["FCX", "NEM", "SCCO", "TECK"],
    "Specialty Chemicals":
        ["APD", "LIN", "ECL", "SHW", "PPG", "DD"],
    "Commodity Chemicals":
        ["DOW", "LYB", "CE", "OLN"],
    "Industrial Gases":
        ["APD", "LIN"],
    "Paper & Forest Products":
        ["IP", "PKG", "GPK", "SEE"],
    "Construction Materials":
        ["VMC", "MLM", "SUM", "EXP"],
    "Steel":
        ["NUE", "STLD", "CLF", "X"],
    "Copper":
        ["FCX", "SCCO"],
    "Gold":
        ["NEM", "GOLD", "AEM", "KGC"],
    "Containers & Packaging":
        ["BLL", "CCK", "AMCR", "IP", "PKG"],
}


def find_sector_peers(sector):
    """Look up peer tickers for a sector, with fuzzy fallback."""
    if sector in SECTOR_PEERS:
        return list(SECTOR_PEERS[sector])
    # Fuzzy: try partial match
    for key, peers in SECTOR_PEERS.items():
        if sector.lower() in key.lower() or key.lower() in sector.lower():
            return list(peers)
    return []


# Ticker-level overrides for issuers whose CREDIT peers don't match their
# GICS sub-industry. AMZN is the classic case: GICS classifies it under
# "Broadline Retail" but its bonds trade alongside the tech hyperscalers
# (GOOGL/MSFT/ORCL/META) which fund similar AI/cloud capex programs.
TICKER_PEER_OVERRIDE = {
    "AMZN": ["GOOGL", "MSFT", "ORCL", "META", "EBAY", "TGT", "COST"],
}


def find_peers_for_ticker(ticker, sector):
    """Override-first peer lookup. Falls back to sector mapping."""
    up = ticker.upper()
    if up in TICKER_PEER_OVERRIDE:
        return list(TICKER_PEER_OVERRIDE[up])
    return find_sector_peers(sector)


# ── Core Payload Builder ───────────────────────────────────────────

def fetch_issuers_bulk(tickers):
    """One ReferenceDataRequest for N tickers, returns
    {ticker: {name, rating, sector, outlook_sp, outlook_mdy, outlook_fitch,
              debt_to_ebitda}}. Cheap — no bond fetch.
    Used by /api/peers (peer-select UI) AND /api/load (peerIssuers map for
    the empirical NIC-outlook regression on the dashboard).
    """
    if not tickers:
        return {}
    secs = [f"{t.upper().strip()} US Equity" for t in tickers]
    data = ref_data(secs, [
        "LONG_COMP_NAME",
        "RTG_SP_LT_LC_ISSUER_CREDIT",
        "GICS_SUB_INDUSTRY_NAME",
        "GICS_INDUSTRY_NAME",
        "RTG_SP_OUTLOOK",
        "RTG_MDY_OUTLOOK",
        "RTG_FITCH_OUTLOOK",
        "TOT_DEBT_TO_EBITDA",
        "NET_DEBT_TO_EBITDA",
    ])
    out = {}
    for tk, sec in zip(tickers, secs):
        info = data.get(sec, {}) or {}
        name = info.get("LONG_COMP_NAME")
        if not name:
            continue
        out[tk.upper().strip()] = {
            "ticker": tk.upper().strip(),
            "name":   name,
            "rating": info.get("RTG_SP_LT_LC_ISSUER_CREDIT") or "NR",
            "sector": (info.get("GICS_SUB_INDUSTRY_NAME")
                       or info.get("GICS_INDUSTRY_NAME")
                       or "Unknown"),
            "outlook_sp":    info.get("RTG_SP_OUTLOOK"),
            "outlook_mdy":   info.get("RTG_MDY_OUTLOOK"),
            "outlook_fitch": info.get("RTG_FITCH_OUTLOOK"),
            "debt_to_ebitda": safe_float(info.get("TOT_DEBT_TO_EBITDA")
                                          or info.get("NET_DEBT_TO_EBITDA")),
        }
    return out


def fetch_issuer_info(ticker):
    """Return issuer dict or {'error': ...}. Cheap — one ReferenceDataRequest.

    Now also pulls rating outlook from S&P / Moody's / Fitch plus leverage
    metrics, so the frontend can compute a data-grounded issuer premium
    suggestion instead of relying purely on analyst judgment.
    """
    ticker = ticker.upper().strip()
    eq_sec = f"{ticker} US Equity"
    eq_data = ref_data([eq_sec], [
        "LONG_COMP_NAME",
        "RTG_SP_LT_LC_ISSUER_CREDIT",
        "GICS_SUB_INDUSTRY_NAME",
        "GICS_INDUSTRY_NAME",
        # Outlook fields — try the standard names; missing fields silently
        # become None via the try/except in ref_data().
        "RTG_SP_OUTLOOK",
        "RTG_MDY_OUTLOOK",
        "RTG_FITCH_OUTLOOK",
        # Leverage — preferred and fallback field names
        "TOT_DEBT_TO_EBITDA",
        "NET_DEBT_TO_EBITDA",
        "TOT_DEBT_TO_TOT_CAP",
    ])
    ii = eq_data.get(eq_sec, {})
    if not ii.get("LONG_COMP_NAME"):
        return {"error": f"Ticker '{ticker}' not found in Bloomberg."}
    return {
        "ticker": ticker,
        "name": ii["LONG_COMP_NAME"],
        "rating": ii.get("RTG_SP_LT_LC_ISSUER_CREDIT") or "NR",
        "sector": ii.get("GICS_SUB_INDUSTRY_NAME")
                  or ii.get("GICS_INDUSTRY_NAME")
                  or "Unknown",
        "outlook_sp":    ii.get("RTG_SP_OUTLOOK"),
        "outlook_mdy":   ii.get("RTG_MDY_OUTLOOK"),
        "outlook_fitch": ii.get("RTG_FITCH_OUTLOOK"),
        "debt_to_ebitda": safe_float(ii.get("TOT_DEBT_TO_EBITDA")
                                      or ii.get("NET_DEBT_TO_EBITDA")),
        "debt_to_cap":   safe_float(ii.get("TOT_DEBT_TO_TOT_CAP")),
    }


def fetch_bonds_for_tickers(peer_tickers):
    """Resolve a list of issuer tickers into a deduplicated list of USD
    bond rows ready for pricing / display.

    Extracted from build_payload Phase C so the CLI can show + let the
    user edit the bond list before pricing. Each call: per-issuer
    OpenFIGI lookup + a single batched Bloomberg ReferenceDataRequest
    over all that issuer's FIGIs.

    Returns list of bond dicts:
      {issuer, ticker, cusip, rating, ytm, spread, issue_dt, maturity_dt,
       orig_tenor, coupon, cds_basis, z_score}
    Sorted ascending by ytm. Bad rows (missing data, wrong currency,
    out-of-range ytm/spread) are silently dropped. z_score is computed
    post-fetch (spread vs same-rating cohort).
    """
    rows = []
    seen = set()
    for ptk in peer_tickers[:8]:  # cap at 8 issuers per call
        try:
            bond_secs = find_issuer_bonds_via_openfigi(ptk, max_bonds=15)
            if not bond_secs:
                continue
            bond_data = ref_data(bond_secs, [
                "YRS_TO_MTY_TDY", "OAS_SPREAD_BID", "Z_SPRD_BID",
                "RTG_SP", "RTG_SP_LT_LC_ISSUER_CREDIT",
                "TICKER", "LONG_COMP_NAME", "CRNCY", "MATURITY", "CPN",
                "ID_CUSIP", "ISSUE_DT", "MATURITY_YEARS_AT_ISSUE",
                "YAS_ZSPREAD_BASIS_CONSTANT_MTY",
                "BB_NEW_ISSUE_SPREAD_ANALYSIS",  # NIA-method NIC at issue
            ])
            for sec, flds in bond_data.items():
                if sec in seen:
                    continue
                seen.add(sec)
                if (flds.get("CRNCY") or "").upper() != "USD":
                    continue
                ytm = years_to_maturity_from_date(flds.get("MATURITY"))
                if ytm is None:
                    ytm = safe_float(flds.get("YRS_TO_MTY_TDY"))
                spread = (safe_float(flds.get("OAS_SPREAD_BID"))
                          or safe_float(flds.get("Z_SPRD_BID")))
                if ytm is None or spread is None:
                    continue
                if ytm < 0.5 or ytm > 35 or spread <= 0:
                    continue
                rating = (flds.get("RTG_SP")
                          or flds.get("RTG_SP_LT_LC_ISSUER_CREDIT")
                          or "NR")
                # Orig tenor: prefer Bloomberg field, else derive from
                # ISSUE_DT + MATURITY (years between).
                orig_tenor = safe_float(flds.get("MATURITY_YEARS_AT_ISSUE"))
                if orig_tenor is None:
                    issue_iso = flds.get("ISSUE_DT")
                    mat_iso = flds.get("MATURITY")
                    if issue_iso and mat_iso:
                        try:
                            i_d = date.fromisoformat(str(issue_iso)[:10])
                            m_d = date.fromisoformat(str(mat_iso)[:10])
                            orig_tenor = round((m_d - i_d).days / 365.25, 1)
                        except (ValueError, TypeError):
                            orig_tenor = None
                rows.append({
                    "issuer":      flds.get("LONG_COMP_NAME") or ptk,
                    "ticker":      flds.get("TICKER") or ptk,
                    "cusip":       flds.get("ID_CUSIP") or "",
                    "rating":      rating,
                    "ytm":         round(ytm, 1),
                    "spread":      round(spread),
                    "issue_dt":    (str(flds.get("ISSUE_DT"))[:10]
                                    if flds.get("ISSUE_DT") else ""),
                    "maturity_dt": (str(flds.get("MATURITY"))[:10]
                                    if flds.get("MATURITY") else ""),
                    "orig_tenor":  orig_tenor,
                    "coupon":      safe_float(flds.get("CPN")),
                    "cds_basis":   safe_float(flds.get("YAS_ZSPREAD_BASIS_CONSTANT_MTY")),
                    "nic_at_issue": safe_float(flds.get("BB_NEW_ISSUE_SPREAD_ANALYSIS")),
                    "z_score":     None,  # filled in below
                })
        except Exception as e:
            sys.stderr.write(f"  Warning: failed to load bonds for {ptk}: {e}\n")
            continue
    rows.sort(key=lambda b: b["ytm"])
    _attach_zscores(rows)
    return rows


# ── Historical outlook lookup + empirical scorecard regression ────
# Used to replace the hardcoded +6 bps "2 of 3 Negative" coefficient with
# an empirical median derived from the loaded peer set's own historical
# NIC payments, bucketed by what each issuer's outlook state was on the
# date each bond was issued.

OUTLOOK_FIELDS = ("RTG_SP_OUTLOOK", "RTG_MDY_OUTLOOK", "RTG_FITCH_OUTLOOK")


def ref_data_with_overrides(securities, fields, overrides):
    """Same shape as ref_data() but with a per-request overrides dict
    (e.g. {"REFERENCE_DATE": "20210315"} to get a historical point-in-time
    value of a refdata field). Used for outlook-at-issue lookups.
    Returns {security: {field: value}}.
    """
    session = get_session()
    svc = session.getService("//blp/refdata")
    req = svc.createRequest("ReferenceDataRequest")
    for s in securities:
        req.append("securities", s)
    for f in fields:
        req.append("fields", f)
    if overrides:
        ov_arr = req.getElement("overrides")
        for k, v in overrides.items():
            ov = ov_arr.appendElement()
            ov.setElement("fieldId", k)
            ov.setElement("value", str(v))
    session.sendRequest(req)

    result = {}
    while True:
        ev = session.nextEvent(10000)
        for msg in ev:
            if msg.hasElement("securityData"):
                arr = msg.getElement("securityData")
                for i in range(arr.numValues()):
                    sd = arr.getValueAsElement(i)
                    sec = sd.getElementAsString("security")
                    d = {}
                    if (not sd.hasElement("securityError")
                            and sd.hasElement("fieldData")):
                        fd = sd.getElement("fieldData")
                        for f in fields:
                            try:
                                el = fd.getElement(f)
                                val = el.getValue()
                                if hasattr(val, "isoformat"):
                                    val = val.isoformat()
                                d[f] = val
                            except Exception:
                                d[f] = None
                    else:
                        for f in fields:
                            d[f] = None
                    result[sec] = d
        if ev.eventType() == blpapi.Event.RESPONSE:
            break
    return result


def _neg_count(outlook_dict):
    """Count how many of the three agencies are on Negative outlook.
    Outlook strings vary in casing ("NEG", "Negative", "STABLE"…); we
    match by substring. Returns int 0..3, or None if no outlook data
    at all (so callers can drop unbucketable rows from the regression).
    """
    if not outlook_dict:
        return None
    vals = [outlook_dict.get("sp"), outlook_dict.get("mdy"),
            outlook_dict.get("fitch")]
    if all(v in (None, "", "NR") for v in vals):
        return None
    n = 0
    for v in vals:
        if v and "NEG" in str(v).upper():
            n += 1
    return n


def fetch_outlook_at_issue(rows, current_outlook_map):
    """Enrich each row with outlook_at_issue: {sp, mdy, fitch, is_proxy}.

    Strategy:
      1. Group rows by (ticker, issue_dt) pairs that have valid dates.
      2. For each unique issue_dt, send ONE ReferenceDataRequest covering
         all tickers issuing on that date, with REFERENCE_DATE override
         set to the issue date — Bloomberg returns the outlook that was
         in effect then.
      3. If the historical lookup returns nothing for a (ticker, date),
         fall back to current_outlook_map[ticker] and mark is_proxy=True.
      4. Rows without an issue_dt are populated entirely from current
         outlook with is_proxy=True.

    Mutates rows in place. Best-effort: any blpapi failure is caught and
    falls back to current-outlook proxy for the whole batch.

    TODO(bql-historical): when BQuant access is wired in, replace the
    REFERENCE_DATE override path with BQL credit_rating_outlook(dates=)
    which is the documented point-in-time source for outlook.
    """
    from collections import defaultdict
    by_date = defaultdict(set)
    for r in rows:
        iss = r.get("issue_dt")
        tk = r.get("ticker")
        if iss and tk:
            by_date[iss].add(tk)

    hist_cache = {}  # (ticker, issue_dt) -> {sp, mdy, fitch}
    for issue_dt, tkrs in by_date.items():
        try:
            yyyymmdd = issue_dt.replace("-", "")[:8]
            secs = [f"{t} US Equity" for t in tkrs]
            res = ref_data_with_overrides(
                secs, list(OUTLOOK_FIELDS),
                {"REFERENCE_DATE": yyyymmdd},
            )
            for sec, flds in res.items():
                tk = sec.split(" ")[0]
                hist_cache[(tk, issue_dt)] = {
                    "sp":    flds.get("RTG_SP_OUTLOOK"),
                    "mdy":   flds.get("RTG_MDY_OUTLOOK"),
                    "fitch": flds.get("RTG_FITCH_OUTLOOK"),
                }
        except Exception as e:
            sys.stderr.write(
                f"  Warning: outlook-at-issue lookup failed for "
                f"{issue_dt}: {e}\n"
            )

    def _proxy_for(tk):
        cur = current_outlook_map.get(tk) or {}
        return {
            "sp":       cur.get("outlook_sp"),
            "mdy":      cur.get("outlook_mdy"),
            "fitch":    cur.get("outlook_fitch"),
            "is_proxy": True,
        }

    for r in rows:
        tk = r.get("ticker")
        iss = r.get("issue_dt")
        hist = hist_cache.get((tk, iss)) if iss else None
        if hist and any(v not in (None, "", "NR") for v in hist.values()):
            r["outlook_at_issue"] = {**hist, "is_proxy": False}
        else:
            r["outlook_at_issue"] = _proxy_for(tk)


def _median(xs):
    """Plain median. Returns None for empty input."""
    xs = [x for x in xs if x is not None]
    if not xs:
        return None
    xs = sorted(xs)
    n = len(xs)
    if n % 2 == 1:
        return xs[n // 2]
    return (xs[n // 2 - 1] + xs[n // 2]) / 2


def compute_empirical_scorecard(rows, current_issuer_outlook=None):
    """Regression-by-bucket: median nic_at_issue grouped by outlook state.

    Buckets rows by neg_count (0, 1, 2, 3) computed from outlook_at_issue.
    Coefficient_N = median(NIC | neg=N) - median(NIC | neg=0). This is
    the empirical "what does the market actually charge for N agencies on
    Negative outlook" — the analog of the hardcoded +6 bps in the scorecard.

    Returns:
      {
        "outlook": {
          "base_median": float | None,   # median NIC for all-Stable bucket
          "coef_1neg": float | None,     # bps premium for 1 of 3 Negative
          "coef_2neg": float | None,     # bps premium for 2 of 3 Negative
          "coef_3neg": float | None,     # bps premium for 3 of 3 Negative
          "n_base": int, "n_1neg": int, "n_2neg": int, "n_3neg": int,
          "proxy_share": float,          # fraction of rows using current-outlook proxy
        },
        "issuer_neg_count": int | None,  # current issuer's neg count, for UI lookup
        "n_total": int,                  # bonds with both nic_at_issue + outlook_at_issue
      }

    Only bonds with both nic_at_issue AND outlook_at_issue contribute.
    """
    buckets = {0: [], 1: [], 2: [], 3: []}
    proxy_count = 0
    n_total = 0
    for r in rows:
        nic = r.get("nic_at_issue")
        oai = r.get("outlook_at_issue")
        if nic is None or oai is None:
            continue
        neg = _neg_count(oai)
        if neg is None:
            continue
        buckets[neg].append(nic)
        n_total += 1
        if oai.get("is_proxy"):
            proxy_count += 1

    base = _median(buckets[0])
    def _coef(bucket_key):
        m = _median(buckets[bucket_key])
        if m is None or base is None:
            return None
        return round(m - base, 1)

    issuer_neg = None
    if current_issuer_outlook:
        issuer_neg = _neg_count({
            "sp":    current_issuer_outlook.get("outlook_sp"),
            "mdy":   current_issuer_outlook.get("outlook_mdy"),
            "fitch": current_issuer_outlook.get("outlook_fitch"),
        })

    return {
        "outlook": {
            "base_median": round(base, 1) if base is not None else None,
            "coef_1neg":   _coef(1),
            "coef_2neg":   _coef(2),
            "coef_3neg":   _coef(3),
            "n_base":      len(buckets[0]),
            "n_1neg":      len(buckets[1]),
            "n_2neg":      len(buckets[2]),
            "n_3neg":      len(buckets[3]),
            "proxy_share": (round(proxy_count / n_total, 2)
                            if n_total > 0 else 0.0),
        },
        "issuer_neg_count": issuer_neg,
        "n_total": n_total,
    }


def _cohort_stats_from(rows):
    """Compute per-rating (mean, std) of spread across rows. Uses sample
    stddev (ddof=1) to match BQL's groupzscore. Returns {rating: (mean,
    std)}; (None, None) for cohorts too small to be meaningful.
    """
    from collections import defaultdict
    buckets = defaultdict(list)
    for r in rows:
        buckets[(r.get("rating") or "NR")].append(r["spread"])
    out = {}
    for rating, spreads in buckets.items():
        n = len(spreads)
        if n < 2:
            out[rating] = (None, None)
            continue
        mean = sum(spreads) / n
        var = sum((s - mean) ** 2 for s in spreads) / (n - 1)  # sample, BQL parity
        std = var ** 0.5
        out[rating] = (mean, std) if std > 0 else (None, None)
    return out


def _attach_zscores(rows, ref_stats=None):
    """Compute spread Z-score per bond vs same-rating cohort. Mutates rows
    in place. Sample stddev (ddof=1). Mirrors BQL's groupzscore(by=rating).

    If ref_stats is provided ({rating: (mean, std)}), z-scores are computed
    against that external cohort instead of from rows themselves — used to
    score the issuer's own bonds against the peer cohort.
    """
    stats = ref_stats if ref_stats is not None else _cohort_stats_from(rows)
    for r in rows:
        rating = r.get("rating") or "NR"
        mean, std = stats.get(rating, (None, None))
        if mean is None or std is None or std <= 0:
            r["z_score"] = 0.0
        else:
            r["z_score"] = round((r["spread"] - mean) / std, 2)


def fetch_issuer_bonds(ticker):
    """Pull the issuer's own outstanding USD bonds. Same shape as
    fetch_bonds_for_tickers, just for a single issuer.
    """
    return fetch_bonds_for_tickers([ticker])


def _build_empirical_scorecard(peer_rows, peer_tickers, issuer_info):
    """Fetch per-peer outlook map, enrich peer bond rows with
    outlook_at_issue, and compute the empirical scorecard.

    Returns (peer_issuers_map, empirical_scorecard). The peer_issuers_map
    is also returned so the UI can show current outlook flags per peer
    in the issuer-premium box.
    """
    try:
        peer_issuers_map = fetch_issuers_bulk(peer_tickers) if peer_tickers else {}
    except Exception as e:
        sys.stderr.write(f"  Warning: peer issuers bulk fetch failed: {e}\n")
        peer_issuers_map = {}

    # Build full outlook map: peers + the issuer being priced. Used as
    # the fallback when historical lookup fails for a (ticker, date).
    outlook_map = dict(peer_issuers_map)
    if issuer_info and issuer_info.get("ticker"):
        outlook_map[issuer_info["ticker"]] = {
            "outlook_sp":    issuer_info.get("outlook_sp"),
            "outlook_mdy":   issuer_info.get("outlook_mdy"),
            "outlook_fitch": issuer_info.get("outlook_fitch"),
        }

    try:
        fetch_outlook_at_issue(peer_rows, outlook_map)
    except Exception as e:
        sys.stderr.write(
            f"  Warning: outlook-at-issue enrichment failed: {e}\n"
        )

    try:
        empirical = compute_empirical_scorecard(
            peer_rows, current_issuer_outlook=issuer_info,
        )
    except Exception as e:
        sys.stderr.write(f"  Warning: empirical scorecard failed: {e}\n")
        empirical = None

    return peer_issuers_map, empirical


def build_payload(ticker, peers_override=None, bonds_override=None, issuer_info=None):
    """Fetch all data from Bloomberg for a given equity ticker.

    Returns dict with keys: issuer, treasuries, peers
    or dict with key: error

    Args:
      ticker:          Equity ticker root.
      peers_override:  Optional explicit peer ticker list. If provided,
                       skips BLOOMBERG_PEERS / sector lookup entirely.
      bonds_override:  Optional pre-built list of bond rows (from
                       fetch_bonds_for_tickers, possibly edited). When
                       provided, skips Phase C entirely. CLI uses this
                       after the user has reviewed/edited the bond list.
      issuer_info:     Optional pre-fetched issuer dict (from fetch_issuer_info).
                       Saves one Bloomberg call when the CLI already has it.
    """
    ticker = ticker.upper().strip()

    # ── Phase A: Issuer info ──
    if issuer_info is None:
        issuer_info = fetch_issuer_info(ticker)
    if "error" in issuer_info:
        return issuer_info

    # ── Phase B: Treasury curve ──
    tsy_secs = [
        "GT2 Govt", "GT5 Govt", "GT7 Govt",
        "GT10 Govt", "GT20 Govt", "GT30 Govt",
    ]
    tsy_tenors = [2, 5, 7, 10, 20, 30]
    # YLD_YTM_MID returns the bond-equivalent yield-to-maturity directly
    # (e.g. 4.556 for GT10 Govt). PX_LAST on these tickers returns the
    # clean price (~98–99), which is what the previous version of this
    # code was incorrectly using — the frontend expects a yield in %.
    tsy_data = ref_data(tsy_secs, ["YLD_YTM_MID"])
    treasuries = {}
    for sec, t in zip(tsy_secs, tsy_tenors):
        val = safe_float(tsy_data.get(sec, {}).get("YLD_YTM_MID"))
        if val is not None:
            treasuries[str(t)] = round(val, 3)

    # ── Phase C: Peer bonds + issuer's own bonds ──
    sector = issuer_info["sector"]
    if bonds_override is not None:
        # Caller already curated the bond list (CLI confirm flow).
        all_peers = list(bonds_override)
        all_peers.sort(key=lambda b: b["ytm"])
        # If override doesn't include z-scores yet, compute them now.
        if any(b.get("z_score") is None for b in all_peers):
            _attach_zscores(all_peers)
        peer_tickers = sorted({b["ticker"] for b in all_peers})
        # Still fetch the issuer's own bonds (separate from curated peer list).
        # Re-score issuer bonds against the PEER cohort so the Z column
        # answers "is this issuer rich/cheap vs peers" not "vs its own bonds".
        try:
            issuer_bonds = fetch_issuer_bonds(ticker)
            _attach_zscores(issuer_bonds, ref_stats=_cohort_stats_from(all_peers))
        except Exception as e:
            sys.stderr.write(f"  Warning: issuer-bond fetch failed for {ticker}: {e}\n")
            issuer_bonds = []
        peer_issuers_map, empirical = _build_empirical_scorecard(
            all_peers, peer_tickers, issuer_info,
        )
        return {
            "issuer":             issuer_info,
            "treasuries":         treasuries,
            "peers":              all_peers,
            "issuerBonds":        issuer_bonds,
            "peerTickers":        peer_tickers,
            "peerIssuers":        peer_issuers_map,
            "empiricalScorecard": empirical,
        }

    if peers_override is not None:
        peer_tickers = [t.upper() for t in peers_override if t.upper() != ticker]
    else:
        peer_tickers = find_peers_for_ticker(ticker, sector)
        peer_tickers = [t for t in peer_tickers if t.upper() != ticker]

    if not peer_tickers:
        return {
            "error": f"No peer mapping for sector '{sector}'. "
                     f"Contact desk to add peers for this sector."
        }

    all_peers = fetch_bonds_for_tickers(peer_tickers)

    # Fetch the issuer's own bonds separately so the frontend can show
    # them as a distinct series + dedicated table. Re-score issuer bonds
    # against the PEER cohort so the Z column tells you "rich/cheap vs
    # peers" rather than "vs other bonds from the same issuer".
    try:
        issuer_bonds = fetch_issuer_bonds(ticker)
        _attach_zscores(issuer_bonds, ref_stats=_cohort_stats_from(all_peers))
    except Exception as e:
        sys.stderr.write(f"  Warning: issuer-bond fetch failed for {ticker}: {e}\n")
        issuer_bonds = []

    peer_issuers_map, empirical = _build_empirical_scorecard(
        all_peers, peer_tickers[:8], issuer_info,
    )
    return {
        "issuer":             issuer_info,
        "treasuries":         treasuries,
        "peers":              all_peers,
        "issuerBonds":        issuer_bonds,
        "peerTickers":        peer_tickers[:8],
        "peerIssuers":        peer_issuers_map,
        "empiricalScorecard": empirical,
    }


# ── Auth helper ────────────────────────────────────────────────────

def _get_auth(handler):
    """Extract bearer token from Authorization header and look up the
    session in cache_store. Returns (email, role) or None.
    """
    h = handler.headers.get("Authorization") or ""
    if not h.lower().startswith("bearer "):
        return None
    token = h[7:].strip()
    sess = cache_store.get_session(token)
    if not sess:
        return None
    return (sess["email"], sess["role"])


# ── HTTP Handler ───────────────────────────────────────────────────

class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        global _session  # may be reset to None on Bloomberg connection error
        # Logo from project directory (BONDPRICING/logo.png)
        if self.path == "/logo.png":
            here = os.path.dirname(os.path.abspath(__file__))
            logo_path = os.path.join(here, "logo.png")
            if os.path.isfile(logo_path):
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                with open(logo_path, "rb") as f:
                    data = f.read()
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return

        # /api/auth/me — returns current user info or 401
        if self.path == "/api/auth/me":
            auth = _get_auth(self)
            if auth is None:
                self._json(401, {"error": "unauthorized"})
                return
            email, role = auth
            self._json(200, {"email": email, "role": role,
                             "cache_configured": cache_store.is_configured()})
            return

        # /api/peers/<TICKER> — cheap: issuer info + suggested peers only.
        # Used by the frontend's two-step flow: user reviews the peer set
        # before triggering the slow bond-fetch via /api/load.
        if self.path.startswith("/api/peers/"):
            auth = _get_auth(self)
            if auth is None:
                self._json(401, {"error": "unauthorized"})
                return
            email, role = auth
            raw = self.path.split("/api/peers/", 1)[1]
            ticker = raw.split("?")[0].strip().upper()
            if not ticker or len(ticker) > 6:
                self._json(400, {"error": "Invalid ticker."})
                return
            # Client role: read-only, cache-only
            if role == "client":
                doc = cache_store.read_cache(ticker)
                if doc and doc.get("peers_response"):
                    self._json(200, doc["peers_response"])
                else:
                    self._json(404, {
                        "error":   "not_cached",
                        "message": "Not yet available — ask your "
                                   "Tigress contact to pull this name.",
                    })
                return
            try:
                issuer = fetch_issuer_info(ticker)
                if "error" in issuer:
                    self._json(404, issuer)
                    return
                # Discover peers (cheap — Bloomberg field + static fallback)
                suggestion = suggest_peers(ticker, issuer["sector"])
                peer_tickers = [p for p in suggestion["peers"] if p != ticker]
                # Bulk fetch name+rating for each peer ticker
                peers_info = fetch_issuers_bulk(peer_tickers)
                # Return peer rows in the original suggested order (only
                # those that resolved successfully in the bulk lookup)
                peers_resolved = []
                for tk in peer_tickers:
                    info = peers_info.get(tk)
                    if info:
                        peers_resolved.append(info)
                response_body = {
                    "issuer":  issuer,
                    "source":  suggestion["source"],
                    "peers":   peers_resolved,
                    "dropped": suggestion.get("dropped", []),
                }
                # Cache the peer suggestion for clients
                cache_store.write_cache(ticker, "peers_response",
                                        response_body, email)
                self._json(200, response_body)
            except ConnectionError as e:
                _session = None
                self._json(503, {"error": str(e)})
            except Exception as e:
                self._json(500, {"error": f"Server error: {e}"})
            return

        # /api/load/<TICKER>?peers=A,B,C  (peers param optional)
        if self.path.startswith("/api/load/"):
            auth = _get_auth(self)
            if auth is None:
                self._json(401, {"error": "unauthorized"})
                return
            email, role = auth
            raw = self.path.split("/api/load/", 1)[1]
            path_part, _, query_part = raw.partition("?")
            ticker = path_part.strip().upper()
            if not ticker or len(ticker) > 6:
                self._json(400, {"error": "Invalid ticker."})
                return
            # Client role: read-only, cache-only. Ignore ?peers= entirely.
            if role == "client":
                doc = cache_store.read_cache(ticker)
                if doc and doc.get("load_response"):
                    self._json(200, doc["load_response"])
                else:
                    self._json(404, {
                        "error":   "not_cached",
                        "message": "Not yet available — ask your "
                                   "Tigress contact to pull this name.",
                    })
                return
            # Parse ?peers=A,B,C if present
            peers_override = None
            if query_part:
                from urllib.parse import parse_qs
                qs = parse_qs(query_part)
                peer_str = (qs.get("peers") or [""])[0]
                if peer_str:
                    peers_override = [p.strip().upper()
                                      for p in peer_str.split(",")
                                      if p.strip()]
            try:
                payload = build_payload(ticker, peers_override=peers_override)
                if "error" in payload:
                    self._json(404, payload)
                else:
                    # Cache the full load payload for clients
                    cache_store.write_cache(
                        ticker, "load_response", payload, email,
                        extra={"peers_used": peers_override or []},
                    )
                    self._json(200, payload)
            except ConnectionError as e:
                _session = None  # force reconnect next time
                self._json(503, {"error": str(e)})
            except Exception as e:
                self._json(500, {"error": f"Server error: {e}"})
            return

        # Static files
        super().do_GET()

    def do_POST(self):
        """Auth endpoints + three Claude AI integration endpoints.

        Auth (no token required for login; logout takes the token):
          POST /api/auth/login          -> {token, email, role}
          POST /api/auth/logout         -> {ok: true}

        AI (token required):
          POST /api/ai/peer-suggestion  -> {peers:[{ticker,decision,reason}], summary, ...}
          POST /api/ai/pitch-commentary -> {exec_summary, market_context, credit_rationale, risk_commentary}
          POST /api/ai/chat             -> {reply, suggested_values?}

        When ANTHROPIC_API_KEY is missing, AI routes return 503 with
        {"error":"not_configured"} so the UI can show a clean message
        instead of crashing.
        """
        # ── Auth routes ──
        if self.path == "/api/auth/login":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except (ValueError, json.JSONDecodeError):
                self._json(400, {"error": "bad_request"})
                return
            email = (body.get("email") or "").strip().lower()
            password = body.get("password") or ""
            result = cache_store.verify_login(email, password)
            if result is None:
                self._json(401, {"error": "invalid_credentials"})
                return
            email, role = result
            token = cache_store.create_session(email, role)
            self._json(200, {"token": token, "email": email, "role": role,
                             "cache_configured": cache_store.is_configured()})
            return

        if self.path == "/api/auth/logout":
            auth = _get_auth(self)
            if auth is not None:
                # Re-extract raw token to delete it (auth has email+role, not token)
                h = self.headers.get("Authorization") or ""
                if h.lower().startswith("bearer "):
                    cache_store.delete_session(h[7:].strip())
            self._json(200, {"ok": True})
            return

        ai_routes = {
            "/api/ai/peer-suggestion":  1024,
            "/api/ai/pitch-commentary": 2048,
            "/api/ai/chat":             1024,
        }
        if self.path in ai_routes:
            # Gate AI routes with auth
            if _get_auth(self) is None:
                self._json(401, {"error": "unauthorized"})
                return
            max_tokens = ai_routes[self.path]
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw.decode("utf-8") or "{}")
            except (ValueError, json.JSONDecodeError) as e:
                self._json(400, {"error": "bad_request", "detail": str(e)})
                return
            prompt = (body.get("prompt") or "").strip()
            if not prompt:
                self._json(400, {"error": "missing_prompt"})
                return
            if len(prompt) > 60000:
                self._json(413, {"error": "prompt_too_long",
                                 "detail": f"{len(prompt)} chars > 60000"})
                return

            import claude_client
            if not claude_client.is_configured():
                self._json(503, {"error": "not_configured",
                                 "detail": "ANTHROPIC_API_KEY not set."})
                return

            res = claude_client.call_claude_json(prompt, max_tokens=max_tokens)
            if not res.get("ok"):
                # Surface the error to the UI but keep status 200 so the
                # frontend can render a graceful inline message rather
                # than throwing on fetch failure. error code is in body.
                self._json(200, {
                    "error":  res.get("error", "unknown"),
                    "detail": res.get("detail", ""),
                })
                return
            # Success: return Claude's parsed JSON directly as the
            # response body — the UI consumes it as-is.
            self._json(200, res["json"])
            return

        self._json(404, {"error": "not_found", "path": self.path})

    def _json(self, code, data):
        body = json.dumps(data, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        # log_error() passes (HTTPStatus, message) — cast to str so the
        # substring check doesn't blow up on the enum.
        first = str(args[0]) if args else ""
        if "/api/" in first:
            sys.stderr.write(f"  API: {first}\n")


# ── Main ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    print(f"\n  Bond Pricing Engine")
    print(f"  http://localhost:{PORT}/")

    print(f"  Bloomberg Terminal: localhost:8194\n")
    try:
        get_session()
        print("  Bloomberg connected.\n")
    except ConnectionError as e:
        print(f"  WARNING: {e}")
        print("  Server starting anyway — API calls will fail until Terminal is running.\n")

    http.server.HTTPServer.allow_reuse_address = True
    httpd = http.server.HTTPServer(("", PORT), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Shutting down.")
        httpd.server_close()
