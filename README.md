# Bond Pricing Engine — Tigress Financial Partners

Internal desk tool for pricing new corporate bond issues using peer comparable analysis and Nelson-Siegel spread curve fitting.

## What It Does

An analyst types in an issuer ticker, the tool pulls peer comps, fits a spread curve, applies credit/issuer/NIC adjustments, and outputs indicative pricing across the maturity spectrum (2Y–30Y). Scenario sliders let you shock spreads, rates, and NIC to stress-test pricing in real time. One click generates a pitch-deck-ready .pptx or data .xlsx.

## How to Run

Open `index.html` in any browser. No server, no install, no dependencies to manage — everything runs client-side.

Also deployed at: [tigress-oere.vercel.app/BONDPRICING](https://tigress-oere.vercel.app/BONDPRICING)

## What's Built (Current State)

### Landing Screen
- Ticker input with validation
- Demo issuers: HUM (Humana), ELV (Elevance), CI (Cigna), MOH (Molina)
- Ready for Bloomberg API integration — just swap the demo validation for a live API call

### Dashboard
- **4 KPI Cards** — 10Y Indicative Spread, 10Y Yield, vs. Base change, Peer count
- **Peer Spread Curve** — Plotly scatter chart with 14 peer bond dots, Nelson-Siegel fitted curve (blue), and issuer indicative points (orange diamonds)
- **Pricing Output Table** — 6 tenors (2Y, 5Y, 7Y, 10Y, 20Y, 30Y) with Treasury, Peer Spread, Credit Adj, Issuer Adj, NIC, Final Spread, Yield, IPT, Guidance
- **10Y Waterfall Chart** — Visual buildup from Treasury base rate through each spread component to all-in yield
- **Sensitivity Matrix** — Heat map of 10Y yield across spread shock x rate shock combinations, current scenario highlighted
- **Peer Bonds Table** — All 14 comparable bonds with issuer, ticker, rating (color-coded), maturity, spread
- **Notes & Flags** — Conditional warnings (risk-off, tight market, high NIC, large deal size) + standard disclaimer

### Sidebar Controls
- Issuer display with Change button
- Deal size slider ($250M–$3,000M)
- IG Spread Shock (-50 to +150 bps)
- Treasury Shock (-100 to +100 bps)
- NIC Adjustment (0–20 bps)
- Preset buttons: Base, Stress, Rally
- Dynamic NIC: deal size >$1.5B auto-adds +1bps, >$2B adds +3bps execution risk premium

### Exports
- **Download XLSX** — 4-tab workbook (Summary, Scenario, Peer Bonds, Methodology)
- **Generate Pitch Deck** — 8-slide branded .pptx:
  1. Title slide (issuer, rating, sector, date)
  2. Executive summary + KPI boxes
  3. Peer spread curve (chart as image)
  4. Pricing table (all tenors)
  5. 10Y waterfall decomposition
  6. Sensitivity matrix
  7. Peer comparable bonds
  8. Disclosures & disclaimers

## Pricing Methodology

### Nelson-Siegel Spread Curve

```
y(t) = B0 + B1 * [(1 - e^(-t/L)) / (t/L)] + B2 * [(1 - e^(-t/L)) / (t/L) - e^(-t/L)]
```

- Lambda (L) fixed at 1.37
- Betas fit via OLS (ordinary least squares) to 14 peer bonds
- With lambda fixed, the model is linear in B0, B1, B2 — solved using the normal equation (no iterative optimizer needed)

### Pricing Math (per tenor)

```
Treasury Yield  = Base Treasury Rate + Rate Shock / 100
Peer Spread     = Base Peer Spread + Spread Shock
Final Spread    = Peer Spread + Credit Adj + Issuer Adj + NIC
Indicative Yield = Treasury Yield + Final Spread / 100
IPT             = Final Spread + 12 bps
Guidance        = Final Spread + 5 bps
```

### What's Hardcoded (Demo Only)

| Data | Source in Production |
|------|---------------------|
| Treasury yields (2Y–30Y) | Bloomberg: YCGT0025 Index |
| Peer bond spreads | Bloomberg: BSRCH + OAS_SPREAD_BID |
| 14 peer comparable bonds | Bloomberg: BSRCH by sector/rating |
| Credit adjustments per tenor | Analyst judgment (stays manual) |
| Issuer adjustments per tenor | Analyst judgment (stays manual) |
| NIC | Analyst judgment (stays manual) |

## Future Scope — Bloomberg API Integration

### Phase 1: Live Market Data
- **Bloomberg BLPAPI** (`blpapi` Python package) on a lightweight backend
- User types ANY ticker → BLPAPI pulls:
  - Issuer info (rating, sector, outstanding debt)
  - Treasury curve (live YCGT0025)
  - Peer bonds auto-discovered via BSRCH (sector + rating + USD + senior unsecured)
  - Real-time OAS spreads from TRACE/Bloomberg
- Nelson-Siegel refits on actual peer data
- Frontend stays the same — just swaps hardcoded JSON for API responses

### Phase 2: AI-Generated Commentary (Claude API)
- **Anthropic Claude API** generates analyst-quality narrative:
  - Executive summary paragraph
  - Market context ("IG spreads are 15bps tight vs 6-month avg")
  - Credit rationale ("BBB, 2 notches below peer median A-")
  - Risk commentary based on scenario
- Rule-based notes (already built) handle real-time flags
- Claude handles the polished write-up for pitch decks

### Phase 3: Enhanced Presentation
- AI-generated text injected into .pptx slides automatically
- Historical spread charts (issuer's spread over last 12 months)
- Relative value analysis (where does this issuer sit vs rating peers?)
- Order book simulation (estimated book size based on deal size + spread)
- PDF export option alongside PPTX and XLSX

### Phase 4: Multi-Asset / Multi-Issuer
- Compare two issuers side-by-side
- Support for High Yield (HY) issuers (different spread dynamics)
- Leveraged loan pricing module
- Cross-currency bond pricing (EUR, GBP)

## Tech Stack

- **Frontend:** Single HTML file, vanilla JS, no framework
- **Charts:** Plotly.js 2.35.2
- **XLSX Export:** SheetJS 0.20.3
- **PPTX Export:** PptxGenJS 3.12.0
- **Deployment:** Vercel (static)
- **Future:** Bloomberg BLPAPI (backend), Anthropic Claude API (AI commentary)

## File Structure

```
BONDPRICING/
  index.html    — The entire application (single file)
  README.md     — This file
```

## Team

Built by the Tigress Financial Partners technology team as an internal desk tool prototype.
