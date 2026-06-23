#!/usr/bin/env python3
"""Bond Pricing Engine - terminal CLI.

Print the same data the HTTP API returns, but as plain text - no charts,
no browser, no server. Reuses build_payload() from server.py and the
NS-curve pricing math from pricing.py, so the CLI shows the same
indicative pricing + sensitivity + notes the frontend does.

Usage:
  python bondcli.py                       # interactive REPL
  python bondcli.py HUM                   # one-shot: HUM, base scenario
  python bondcli.py HUM AAPL XOM          # one-shot multiple tickers
  python bondcli.py HUM --preset=stress   # named scenario preset
  python bondcli.py HUM --spread-shock=25 --nic=8 --deal-size=2500

Scenario flags (all optional, override the preset):
  --preset=base|stress|rally   pick a preset (default: base)
  --spread-shock=N             peer spread shock in bps (e.g. -20, 75)
  --rate-shock=N               treasury shock in bps   (e.g. -50, 25)
  --nic=N                      base new-issue concession in bps
  --issuer-premium=N           issuer-specific premium in bps
  --deal-size=N                deal size in $M (>1500 triggers big-deal bump)
  --no-sens                    skip the sensitivity grid
  --no-pricing                 skip pricing + sensitivity + notes (raw only)

Peer-set flags:
  --peers=A,B,C                seed bond fetch with these tickers (skips Bloomberg suggest)
  --no-confirm                 accept the fetched bond list as-is (skips edit prompt)

Interactive flow per ticker:
  1. Bloomberg suggests peers (US-filtered) -> shown
  2. Bonds fetched for those peers -> shown numbered
  3. Edit prompt: -3 (remove row), -CNDT (remove issuer), +ORCL (add issuer's bonds)
  4. Accept -> pricing runs against the curated bond list

In REPL mode, type a ticker and press Enter. Append flags after the
ticker to override the scenario for that fetch, e.g.:
  ticker> HUM --preset=stress
  ticker> AAPL --spread-shock=50 --deal-size=3000

Env:
  OPENFIGI_API_KEY   optional, lifts OpenFIGI rate limit from 25 to 250 req/min
"""
import sys
from collections import defaultdict

from pricing import (
    PRESETS,
    TENORS,
    default_scenario,
    compute_pricing,
    compute_sensitivity,
    compute_notes,
)


def parse_flags(tokens):
    """Pull --foo=bar tokens off the front/back of a token list.

    Returns (scenario_dict, show_pricing, show_sens, leftover_tokens).
    Unknown flags raise ValueError so a typo doesn't silently become a ticker.
    """
    scenario = default_scenario()
    show_pricing = True
    show_sens = True
    peers_override = None
    no_confirm = False
    leftover = []
    int_keys = {
        "--spread-shock":   "spread_shock",
        "--rate-shock":     "rate_shock",
        "--nic":            "nic",
        "--issuer-premium": "issuer_premium",
        "--deal-size":      "deal_size",
    }
    for tok in tokens:
        if not tok.startswith("--"):
            leftover.append(tok)
            continue
        if tok == "--no-sens":
            show_sens = False
            continue
        if tok == "--no-pricing":
            show_pricing = False
            continue
        if tok == "--no-confirm":
            no_confirm = True
            continue
        if "=" not in tok:
            raise ValueError(f"flag '{tok}' needs =VALUE")
        key, _, val = tok.partition("=")
        if key == "--preset":
            if val not in PRESETS:
                raise ValueError(f"unknown preset '{val}'; choose base|stress|rally")
            scenario.update(PRESETS[val])
            continue
        if key == "--peers":
            peers_override = [p.strip().upper() for p in val.split(",") if p.strip()]
            if not peers_override:
                raise ValueError("--peers must list at least one ticker")
            continue
        if key in int_keys:
            try:
                scenario[int_keys[key]] = int(val)
            except ValueError:
                raise ValueError(f"flag '{key}' needs an integer, got '{val}'")
            continue
        raise ValueError(f"unknown flag '{key}'")
    return {
        "scenario":         scenario,
        "show_pricing":     show_pricing,
        "show_sens":        show_sens,
        "peers_override":   peers_override,
        "no_confirm":       no_confirm,
        "tickers":          leftover,
    }


def _print_bond_table(bonds):
    """Numbered bond table for the confirm prompt. Compact view used during
    peer-list edit — full per-bond detail (CUSIP/issue_dt/coupon/z) shows
    after pricing in render().
    """
    if not bonds:
        print("  (no bonds)")
        return
    print(f"  {'#':>3}  {'Issuer':<30} {'Tkr':<6} {'Rtg':<5} {'YTM':>6} {'Sprd':>8}")
    print(f"  {'-'*3}  {'-'*30} {'-'*6} {'-'*5} {'-'*6} {'-'*8}")
    for i, b in enumerate(bonds, 1):
        nm = (b["issuer"] or "")[:30]
        print(f"  {i:>3}  {nm:<30} {b['ticker']:<6} {b['rating']:<5} "
              f"{b['ytm']:>5.1f}y {b['spread']:>6}b")


def _print_bond_detail_table(bonds, header_label):
    """Full per-bond detail table including CUSIP / issue_dt / coupon / cds /
    z-score columns. Shown after pricing for both peer and issuer-own bond
    sets.
    """
    if not bonds:
        print(f"\n{header_label}: (none)\n")
        return
    print(f"\n{header_label} ({len(bonds)})")
    print(f"  {'Issuer':<28} {'Tkr':<6} {'CUSIP':<10} {'Rtg':<4} "
          f"{'Issued':<10} {'Matures':<10} {'Orig':>4} {'Mat':>5} "
          f"{'Coupon':>7} {'Sprd':>6} {'CDS':>6} {'Z':>6}")
    print(f"  {'-'*28} {'-'*6} {'-'*10} {'-'*4} {'-'*10} {'-'*10} "
          f"{'-'*4} {'-'*5} {'-'*7} {'-'*6} {'-'*6} {'-'*6}")
    for b in bonds:
        nm   = (b.get("issuer") or "")[:28]
        tk   = (b.get("ticker") or "")[:6]
        cus  = (b.get("cusip") or "-")[:10]
        rtg  = (b.get("rating") or "NR")[:4]
        idt  = (b.get("issue_dt") or "-")[:10]
        mdt  = (b.get("maturity_dt") or "-")[:10]
        ot   = b.get("orig_tenor")
        ot_s = f"{ot:.0f}y" if isinstance(ot, (int, float)) else "-"
        ytm  = b.get("ytm")
        ytm_s = f"{ytm:.1f}y" if isinstance(ytm, (int, float)) else "-"
        cpn  = b.get("coupon")
        cpn_s = f"{cpn:.3f}%" if isinstance(cpn, (int, float)) else "-"
        sp_s  = f"{b.get('spread', 0)}b"
        cds   = b.get("cds_basis")
        cds_s = f"{cds:+.1f}" if isinstance(cds, (int, float)) else "-"
        z     = b.get("z_score")
        z_s   = f"{z:+.2f}" if isinstance(z, (int, float)) else "-"
        print(f"  {nm:<28} {tk:<6} {cus:<10} {rtg:<4} "
              f"{idt:<10} {mdt:<10} {ot_s:>4} {ytm_s:>5} "
              f"{cpn_s:>7} {sp_s:>6} {cds_s:>6} {z_s:>6}")


def confirm_bonds(bonds, fetch_bonds_for_tickers, no_confirm=False):
    """Show the peer-bond list and let the user prune/extend before pricing.

    Returns the final bond list, or None to abort this ticker.

    Edit grammar at the prompt:
      <empty> | y | yes | ok         -> accept as-is, run pricing
      n | no | skip | quit           -> skip this ticker
      -N  (e.g. "-3" or "-3 -5 -7")  -> remove bond by row number
      -TICKER (e.g. "-CNDT")         -> remove ALL bonds for that issuer
      +TICKER (e.g. "+ORCL")         -> fetch this issuer's bonds and add
      mixed: "-CNDT -UIS +ORCL +HPQ" works fine
    """
    bonds = list(bonds)
    print(f"\nPEER BONDS ({len(bonds)} found, before edit)")
    _print_bond_table(bonds)

    if no_confirm:
        print("  (--no-confirm: using bond list as-is)")
        return bonds

    print("\n  Edit the bond list?")
    print("    [Enter] / y           = accept and price")
    print("    n                     = skip this ticker")
    print("    -N -M                 = remove by row #   (e.g. -3 -5)")
    print("    -TICKER               = remove all bonds for issuer (e.g. -CNDT)")
    print("    +TICKER               = add an issuer's bonds        (e.g. +ORCL)")
    print("  (combine freely: '-CNDT -UIS +ORCL +HPQ')")

    while True:
        try:
            raw = input("  bonds> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if raw == "" or raw.lower() in {"y", "yes", "ok"}:
            return bonds
        if raw.lower() in {"n", "no", "skip", "q", "quit"}:
            return None

        tokens = raw.split()
        bad = [t for t in tokens if not t or t[0] not in "+-"]
        if bad:
            print(f"  (need every token to start with + or -; bad: {', '.join(bad)})")
            continue

        removed_rows = set()
        removed_tkrs = set()
        added_tkrs = []
        for tok in tokens:
            op, body = tok[0], tok[1:].strip().upper()
            if not body:
                continue
            if op == "-":
                if body.isdigit():
                    idx = int(body)
                    if 1 <= idx <= len(bonds):
                        removed_rows.add(idx - 1)
                    else:
                        print(f"  (row {idx} out of range 1..{len(bonds)} — ignored)")
                else:
                    removed_tkrs.add(body)
            elif op == "+":
                if body.isdigit():
                    print(f"  (can't '+{body}': use +TICKER to add an issuer)")
                else:
                    added_tkrs.append(body)

        # Apply removals
        if removed_rows or removed_tkrs:
            new_bonds = []
            for i, b in enumerate(bonds):
                if i in removed_rows:
                    continue
                if b["ticker"].upper() in removed_tkrs:
                    continue
                new_bonds.append(b)
            bonds = new_bonds

        # Apply additions (Bloomberg + OpenFIGI fetch — slow, surface what we're doing)
        if added_tkrs:
            existing_tkrs = {b["ticker"].upper() for b in bonds}
            to_fetch = [t for t in added_tkrs if t not in existing_tkrs]
            if to_fetch:
                print(f"  fetching bonds for: {', '.join(to_fetch)} ...")
                new_rows = fetch_bonds_for_tickers(to_fetch)
                if new_rows:
                    print(f"  added {len(new_rows)} bond(s)")
                    bonds.extend(new_rows)
                    bonds.sort(key=lambda b: b["ytm"])
                else:
                    print(f"  (no bonds found for {', '.join(to_fetch)})")

        print(f"\n  -> {len(bonds)} bond(s) after edit:")
        _print_bond_table(bonds)
        if not bonds:
            print("  (list is empty; type +TICKER to add or n to skip)")
            continue
        print("  Accept now? [Enter]/y, or edit again:")


def usage():
    sys.stderr.write(__doc__)
    sys.exit(1)


def process(ticker, scenario=None, show_pricing=True, show_sens=True,
            peers_override=None, no_confirm=False):
    """Fetch + render one ticker. Returns True on success, False on error.

    Flow:
      A. issuer info (cheap)
      B. suggest peer tickers (Bloomberg US-filtered, sector fallback)
      C. fetch bonds for those tickers
      D. show bond list, let user prune/add (confirm_bonds)
      E. build payload from the curated bond list, render + price
    """
    from server import (fetch_issuer_info, suggest_peers,  # noqa: E402
                        fetch_bonds_for_tickers, build_payload)

    # A. issuer info
    issuer = fetch_issuer_info(ticker)
    if "error" in issuer:
        sys.stderr.write(f"ERROR: {issuer['error']}\n\n")
        return False
    print(f"\nISSUER: {ticker}  {issuer['name']}  ({issuer.get('rating','NR')}, {issuer.get('sector','?')})")

    # B. seed peer-ticker list
    if peers_override is not None:
        seed_tickers = peers_override
        print(f"PEER SEED (--peers): {', '.join(seed_tickers)}")
    else:
        suggestion = suggest_peers(ticker, issuer["sector"])
        src_label = {
            "bloomberg": "Bloomberg BLOOMBERG_PEERS (US-filtered)",
            "sector":    "static sector mapping (Bloomberg returned nothing usable)",
            "override":  "hand-curated ticker override",
        }.get(suggestion["source"], suggestion["source"])
        print(f"PEER SEED ({src_label}): {', '.join(suggestion['peers']) or '(none)'}")
        if suggestion["dropped"]:
            print(f"  (dropped {len(suggestion['dropped'])} non-US: "
                  f"{', '.join(suggestion['dropped'][:8])}"
                  f"{', ...' if len(suggestion['dropped']) > 8 else ''})")
        seed_tickers = suggestion["peers"]
        if not seed_tickers:
            sys.stderr.write("  no peers — provide --peers=A,B,C or update sector map\n\n")
            return False

    # C. fetch bonds (OpenFIGI + Bloomberg, slow)
    print("Fetching bonds...")
    bonds = fetch_bonds_for_tickers(seed_tickers)

    # D. bond-level confirm + edit
    final_bonds = confirm_bonds(bonds, fetch_bonds_for_tickers, no_confirm=no_confirm)
    if final_bonds is None:
        print(f"  (skipped {ticker})\n")
        return False
    if not final_bonds:
        sys.stderr.write("  no bonds after edit — cannot price\n\n")
        return False

    # E. build payload with curated bonds, render, price
    payload = build_payload(ticker, bonds_override=final_bonds, issuer_info=issuer)
    if "error" in payload:
        sys.stderr.write(f"ERROR: {payload['error']}\n\n")
        return False
    render(payload)
    if show_pricing:
        sc = scenario if scenario is not None else default_scenario()
        try:
            pricing = compute_pricing(payload, sc)
            render_pricing(pricing, sc)
            if show_sens:
                sens = compute_sensitivity(payload, sc)
                render_sensitivity(sens)
            notes = compute_notes(sc, pricing["effective_nic"], pricing["nic_bump"])
            render_notes(notes)
        except Exception as e:
            sys.stderr.write(f"  (pricing skipped: {e})\n\n")
    return True


def repl(_unused=None):
    """Interactive loop: prompt, fetch, render, repeat until quit/EOF."""
    print("Bond Pricing Engine - interactive mode")
    print("Type a ticker (e.g. HUM, JPM, AAPL) and press Enter.")
    print("Type 'quit' or 'exit' to leave (Ctrl-D / Ctrl-C also works).\n")
    # Stray quotes, semicolons, trailing commas etc. should not leak into
    # the Bloomberg query — strip them from both ends. Internal dots stay
    # so tickers like BRK.B / BF.B still work.
    STRAY = " '\"`;,.\t\r\n"
    while True:
        try:
            raw = input("ticker> ").strip(STRAY)
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not raw:
            continue
        if raw.lower() in {"quit", "exit", "q"}:
            return
        # Allow inline flags: "HUM --preset=stress --nic=8 --peers=ORCL,HPQ,CSCO"
        tokens = raw.split()
        try:
            opts = parse_flags(tokens)
        except ValueError as e:
            sys.stderr.write(f"  (bad flag: {e})\n\n")
            continue
        rest = opts["tickers"]
        if not rest:
            continue
        ticker = rest[0].upper()
        if len(ticker) > 6:
            sys.stderr.write(f"  (skipping '{rest[0]}': too long for a ticker)\n\n")
            continue
        print()
        process(ticker, opts["scenario"], opts["show_pricing"], opts["show_sens"],
                opts["peers_override"], opts["no_confirm"])


def main(argv):
    args = argv[1:]

    if not args:
        repl(None)
        return

    try:
        opts = parse_flags(args)
    except ValueError as e:
        sys.stderr.write(f"ERROR: {e}\n")
        sys.exit(2)

    if not opts["tickers"]:
        usage()

    failed = False
    for ticker in opts["tickers"]:
        ok = process(
            ticker.upper().strip(),
            opts["scenario"], opts["show_pricing"], opts["show_sens"],
            opts["peers_override"], opts["no_confirm"],
        )
        if not ok:
            failed = True
    if failed:
        sys.exit(2)


def render(p):
    issuer = p["issuer"]
    treasuries = p["treasuries"]
    peers = p["peers"]

    bar = "=" * 72
    print(bar)
    print("BOND PRICING ENGINE")
    print(bar)

    # ── Issuer ──
    print("\nISSUER")
    print(f"  {issuer['ticker']:6}  {issuer['name']}")
    print(f"  Rating: {issuer.get('rating', 'NR'):<6}  Sector: {issuer.get('sector', '?')}")

    # ── Treasury curve ──
    print("\nTREASURY YIELDS")
    for tenor in [2, 5, 7, 10, 20, 30]:
        v = treasuries.get(str(tenor))
        if v is None:
            print(f"  {tenor:>3}Y   (n/a)")
        else:
            print(f"  {tenor:>3}Y   {v:>6.3f} %")

    # ── Issuer's own bonds (current outstanding) ──
    issuer_bonds = p.get("issuerBonds") or []
    _print_bond_detail_table(
        sorted(issuer_bonds, key=lambda r: r["ytm"]),
        f"{issuer['ticker']} OWN BONDS",
    )

    # ── Peer bonds ──
    if not peers:
        print("\nPEER BONDS: (none)")
    else:
        rows = sorted(peers, key=lambda r: r["ytm"])
        _print_bond_detail_table(rows, "PEER BONDS")

        # Summary by tenor bucket
        buckets = defaultdict(list)
        for r in rows:
            y = r["ytm"]
            if y < 3:    b = "<3y"
            elif y < 6:  b = "3-6y"
            elif y < 11: b = "6-11y"
            elif y < 21: b = "11-21y"
            else:        b = "21y+"
            buckets[b].append(r["spread"])

        print("\nSPREAD SUMMARY BY MATURITY BUCKET")
        print(f"  {'Bucket':<8} {'N':>3} {'Min':>6} {'Median':>7} {'Mean':>7} {'Max':>6}")
        print(f"  {'-'*8} {'-'*3} {'-'*6} {'-'*7} {'-'*7} {'-'*6}")
        for b in ["<3y", "3-6y", "6-11y", "11-21y", "21y+"]:
            if b in buckets:
                vals = sorted(buckets[b])
                n = len(vals)
                mid = vals[n // 2] if n else 0
                mean = sum(vals) / n if n else 0
                print(f"  {b:<8} {n:>3} {vals[0]:>4} bps {mid:>4} bps {mean:>4.0f} bps {vals[-1]:>4} bps")

    # ── Peer tickers attempted ──
    pt = p.get("peerTickers", [])
    if pt:
        print(f"\nPEER TICKERS QUERIED: {', '.join(pt)}")

    print()


def render_pricing(p, scenario):
    rows = p["rows"]
    print("INDICATIVE PRICING")
    print(f"  scenario: spread_shock={scenario['spread_shock']:+d}bps  "
          f"rate_shock={scenario['rate_shock']:+d}bps  "
          f"nic={scenario['nic']}bps  "
          f"issuer_prem={scenario['issuer_premium']}bps  "
          f"deal_size=${scenario['deal_size']/1000:.1f}B")
    if p["nic_bump"]:
        print(f"  effective NIC: {p['effective_nic']} bps (base + big-deal bump of +{p['nic_bump']})")
    stat = p.get("nic_suggested")
    if stat:
        method_label = ("Bloomberg NIA-derived (median across cohort)"
                        if stat["method"] == "BB_NEW_ISSUE_SPREAD_ANALYSIS"
                        else "residual dispersion (p75)")
        print(f"  statistical NIC: {stat['value']} bps "
              f"[method={method_label}, n={stat['n']}, raw={stat['raw']}]")
    b = p["beta"]
    print(f"  NS fit: beta0={b[0]:.1f}  beta1={b[1]:.1f}  beta2={b[2]:.1f}")
    print()
    print(f"  {'Tenor':<6} {'Tsy':>6} {'Peer':>5} {'Cred':>5} {'Iss':>5} "
          f"{'NIC':>4} {'Sprd':>6} {'Yield':>7} {'IPT':>5} {'Guid':>5}")
    print(f"  {'-'*6} {'-'*6} {'-'*5} {'-'*5} {'-'*5} {'-'*4} {'-'*6} {'-'*7} {'-'*5} {'-'*5}")
    for r in rows:
        print(f"  {r['label']:<6} {r['treasury']:>5.3f}% "
              f"{r['peer']:>4}b {r['credit']:>+4}b {r['issuer']:>+4}b {r['nic']:>3}b "
              f"{r['final_spread']:>4.0f}b {r['yield']:>6.3f}% "
              f"{r['ipt']:>3.0f}b {r['guidance']:>3.0f}b")
    print()

    # 10Y waterfall
    r10 = next((r for r in rows if r["tenor"] == 10), None)
    if r10:
        print("  10Y DECOMPOSITION")
        tsy_yld = r10["treasury"]
        print(f"    Treasury base                    {tsy_yld:>6.3f} %")
        print(f"  + Peer NS spread       {r10['peer']:>+4} bps")
        print(f"  + Credit adj           {r10['credit']:>+4} bps")
        print(f"  + Issuer adj           {r10['issuer']:>+4} bps")
        print(f"  + NIC                  {r10['nic']:>+4} bps")
        print(f"  = Final spread         {r10['final_spread']:>+4.0f} bps")
        print(f"    All-in yield                     {r10['yield']:>6.3f} %")
        print()


def render_sensitivity(s):
    print("SENSITIVITY (10Y all-in yield, %)")
    rate_hdr = "  spread\\rate | " + " ".join(f"{r:>+5}" for r in s["rate_shocks"])
    print(rate_hdr)
    print(f"  {'-'*11}-+-{'-'*(6*len(s['rate_shocks'])-1)}")
    for spr, row in zip(s["spread_shocks"], s["grid"]):
        marker = "*" if spr == s["current_spread"] else " "
        cells = " ".join(f"{v:>5.2f}" for v in row)
        print(f"  {marker}{spr:>+5}bps    | {cells}")
    print(f"  (* = current spread_shock; current rate_shock={s['current_rate']:+d}bps)")
    print()


def render_notes(notes):
    if not notes:
        return
    print("NOTES")
    tag = {"warn": "[!] ", "info": "[i] ", "ok": "[+] ", "default": "    "}
    for kind, msg in notes:
        print(f"  {tag.get(kind, '    ')}{msg}")
    print()


if __name__ == "__main__":
    try:
        main(sys.argv)
    except KeyboardInterrupt:
        sys.stderr.write("\nInterrupted.\n")
        sys.exit(130)
