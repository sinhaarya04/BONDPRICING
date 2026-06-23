"""One-off probe: which Bloomberg peer fields actually return data?

Sends a single ReferenceDataRequest for IBM US Equity asking for every
plausible peer-list field. Prints what each field returned (array of
tickers, None, fieldError, security_error). Decides whether Tier A
(Bloomberg peer field) is viable on this seat before we touch server.py.

Run from C:\\Users\\asinha\\Desktop\\BONDPRICING with Bloomberg Terminal
logged in on localhost:8194.
"""
import sys
import blpapi


CANDIDATE_FIELDS = [
    # Most commonly cited peer fields in Bloomberg FLDS
    "EQY_PEER_TICKERS",
    "BLOOMBERG_PEERS_OVERRIDE_TICKERS",
    "RELATIVE_VALUATION_PEERS",
    # Some seats expose peers as a single-string bulk field
    "BLOOMBERG_PEERS",
    "PEER_GROUP",
    # Industry granularity for fallback / Tier B
    "INDUSTRY_GROUP",
    "GICS_SUB_INDUSTRY_NAME",
    "BLOOMBERG_INDUSTRY_NAME",
    # Sanity check — known-working field
    "NAME",
]


def open_session():
    opts = blpapi.SessionOptions()
    opts.setServerHost("localhost")
    opts.setServerPort(8194)
    s = blpapi.Session(opts)
    if not s.start():
        sys.exit("Could not start blpapi session (is Bloomberg Terminal running?)")
    if not s.openService("//blp/refdata"):
        sys.exit("Could not open //blp/refdata")
    return s


def probe(ticker="IBM US Equity"):
    s = open_session()
    try:
        svc = s.getService("//blp/refdata")
        req = svc.createRequest("ReferenceDataRequest")
        req.append("securities", ticker)
        for f in CANDIDATE_FIELDS:
            req.append("fields", f)
        s.sendRequest(req)

        results = {}
        field_errors = {}
        sec_errors = []
        done = False
        while not done:
            ev = s.nextEvent(5000)
            for msg in ev:
                if not msg.hasElement("securityData"):
                    continue
                sds = msg.getElement("securityData")
                for i in range(sds.numValues()):
                    sd = sds.getValue(i)
                    if sd.hasElement("securityError"):
                        sec_errors.append(sd.getElement("securityError").toString())
                        continue
                    if sd.hasElement("fieldExceptions"):
                        fx = sd.getElement("fieldExceptions")
                        for j in range(fx.numValues()):
                            fe = fx.getValue(j)
                            fid = fe.getElement("fieldId").getValueAsString()
                            err = fe.getElement("errorInfo").getElement("message").getValueAsString()
                            field_errors[fid] = err
                    if sd.hasElement("fieldData"):
                        fd = sd.getElement("fieldData")
                        for f in CANDIDATE_FIELDS:
                            if not fd.hasElement(f):
                                continue
                            el = fd.getElement(f)
                            # Bulk (array) field vs scalar
                            if el.isArray():
                                vals = []
                                for k in range(el.numValues()):
                                    row = el.getValue(k)
                                    # Bulk peer rows may have a single sub-element
                                    if row.numElements() > 0:
                                        # take all sub-element values as strings
                                        parts = []
                                        for m in range(row.numElements()):
                                            sub = row.getElement(m)
                                            parts.append(sub.getValueAsString())
                                        vals.append("|".join(parts))
                                    else:
                                        vals.append(row.getValueAsString())
                                results[f] = ("ARRAY", vals)
                            else:
                                results[f] = ("SCALAR", el.getValueAsString())
            if ev.eventType() == blpapi.Event.RESPONSE:
                done = True
    finally:
        s.stop()

    print("=" * 72)
    print(f"PROBE: {ticker}")
    print("=" * 72)
    if sec_errors:
        print("SECURITY ERRORS:")
        for e in sec_errors:
            print(f"  {e}")
        print()

    print("FIELDS THAT RETURNED DATA:")
    for f in CANDIDATE_FIELDS:
        if f in results:
            kind, val = results[f]
            if kind == "ARRAY":
                print(f"  [OK] {f}  (ARRAY, {len(val)} entries)")
                for v in val[:20]:
                    print(f"        - {v}")
                if len(val) > 20:
                    print(f"        ... +{len(val)-20} more")
            else:
                print(f"  [OK] {f}  (SCALAR) = {val}")
        elif f in field_errors:
            print(f"  [ERR] {f}  -> {field_errors[f]}")
        else:
            print(f"  [--] {f}  (no data, no error)")
    print()


if __name__ == "__main__":
    target = sys.argv[1] + " US Equity" if len(sys.argv) > 1 else "IBM US Equity"
    probe(target)
