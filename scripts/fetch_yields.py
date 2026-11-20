#!/usr/bin/env python3
"""Fetch sovereign yield curves and write data/yields.json.

Sources (all free, no API key):
  US  U.S. Department of the Treasury, daily par yield curve (CSV per year)
  CA  Bank of Canada Valet API, treasury bills + benchmark bond yields
  EU  ECB Data Portal, euro area AAA government bond spot curve (Svensson fit)

Output layout:
  tenors           shared tenor labels
  months           sorted list of YYYY-MM
  monthly[code]    list aligned to months; each item is an 11-value curve
                   (month-end observation) or null
  daily[code]      {dates: [...], curves: [...]} for the most recent ~400 days
  meta[code]       name, source, url, latest date, notes
Stdlib only so it runs anywhere.
"""
import csv
import io
import json
import os
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone

TENORS = ["1M", "3M", "6M", "1Y", "2Y", "3Y", "5Y", "7Y", "10Y", "20Y", "30Y"]
START_YEAR = 2000
DAILY_KEEP_DAYS = 400
UA = "Mozilla/5.0 (yield-curve-monitor; +https://github.com/Moezusb/yield-curve-monitor)"
OUT = os.path.join(os.path.dirname(__file__), "..", "data", "yields.json")


def get(url, tries=4, timeout=60):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8-sig", errors="replace")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"GET failed {url}: {last}")


def num(v):
    try:
        f = float(str(v).strip())
        return round(f, 3)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- US Treasury
US_COLS = {
    "1M": ["1 Mo"], "3M": ["3 Mo"], "6M": ["6 Mo"], "1Y": ["1 Yr"], "2Y": ["2 Yr"],
    "3Y": ["3 Yr"], "5Y": ["5 Yr"], "7Y": ["7 Yr"], "10Y": ["10 Yr"],
    "20Y": ["20 Yr"], "30Y": ["30 Yr"],
}


def fetch_us():
    daily = {}
    this_year = date.today().year
    for y in range(START_YEAR, this_year + 1):
        url = (
            "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
            f"daily-treasury-rates.csv/{y}/all?type=daily_treasury_yield_curve"
            f"&field_tdr_date_value={y}&page&_format=csv"
        )
        try:
            text = get(url)
        except RuntimeError as e:
            print("  US", y, "failed:", e)
            continue
        rows = list(csv.DictReader(io.StringIO(text)))
        for row in rows:
            d = row.get("Date")
            if not d:
                continue
            try:
                iso = datetime.strptime(d.strip(), "%m/%d/%Y").date().isoformat()
            except ValueError:
                continue
            curve = []
            for t in TENORS:
                v = None
                for col in US_COLS[t]:
                    if col in row:
                        v = num(row[col])
                        break
                curve.append(v)
            if sum(v is not None for v in curve) >= 6:
                daily[iso] = curve
        print(f"  US {y}: {len(rows)} rows")
    return daily


# ------------------------------------------------------------ Bank of Canada
CA_SERIES = {
    "1M": "TB.CDN.30D.MID", "3M": "TB.CDN.90D.MID", "6M": "TB.CDN.180D.MID",
    "1Y": "TB.CDN.1Y.MID", "2Y": "BD.CDN.2YR.DQ.YLD", "3Y": "BD.CDN.3YR.DQ.YLD",
    "5Y": "BD.CDN.5YR.DQ.YLD", "7Y": "BD.CDN.7YR.DQ.YLD", "10Y": "BD.CDN.10YR.DQ.YLD",
    "30Y": "BD.CDN.LONG.DQ.YLD",
}


def fetch_ca():
    ids = ",".join(CA_SERIES.values())
    url = f"https://www.bankofcanada.ca/valet/observations/{ids}/json?start_date={START_YEAR}-01-01"
    j = json.loads(get(url))
    obs = j.get("observations", [])
    daily = {}
    for o in obs:
        d = o.get("d")
        curve = []
        for t in TENORS:
            sid = CA_SERIES.get(t)
            cell = o.get(sid) if sid else None
            curve.append(num(cell.get("v")) if isinstance(cell, dict) else None)
        if sum(v is not None for v in curve) >= 5:
            daily[d] = curve
    print(f"  CA: {len(obs)} observations, {len(daily)} usable days")
    return daily


# ------------------------------------------------------------------- ECB
EU_TENORS = {"3M": "SR_3M", "6M": "SR_6M", "1Y": "SR_1Y", "2Y": "SR_2Y", "3Y": "SR_3Y",
             "5Y": "SR_5Y", "7Y": "SR_7Y", "10Y": "SR_10Y", "20Y": "SR_20Y", "30Y": "SR_30Y"}


def fetch_eu():
    per_tenor = {}
    for t, code in EU_TENORS.items():
        url = (
            "https://data-api.ecb.europa.eu/service/data/YC/"
            f"B.U2.EUR.4F.G_N_A.SV_C_YM.{code}?startPeriod=2004-09-01&format=csvdata"
        )
        try:
            text = get(url)
        except RuntimeError as e:
            print("  EU", t, "failed:", e)
            continue
        series = {}
        for row in csv.DictReader(io.StringIO(text)):
            v = num(row.get("OBS_VALUE"))
            if v is not None and row.get("TIME_PERIOD"):
                series[row["TIME_PERIOD"]] = v
        per_tenor[t] = series
        print(f"  EU {t}: {len(series)} obs")
    dates = sorted(set().union(*[s.keys() for s in per_tenor.values()])) if per_tenor else []
    daily = {}
    for d in dates:
        curve = [per_tenor.get(t, {}).get(d) for t in TENORS]
        if sum(v is not None for v in curve) >= 6:
            daily[d] = curve
    return daily


# ------------------------------------------------------------- assemble
def month_end(daily):
    out = {}
    for d in sorted(daily):
        out[d[:7]] = daily[d]  # later dates overwrite: last obs of month wins
    return out


def main():
    prev = {}
    if os.path.exists(OUT):
        with open(OUT) as f:
            prev = json.load(f)

    fetchers = {"us": fetch_us, "ca": fetch_ca, "eu": fetch_eu}
    dailies = {}
    for code, fn in fetchers.items():
        print(f"Fetching {code.upper()}")
        try:
            dailies[code] = fn()
        except Exception as e:  # noqa: BLE001
            print(f"  {code} FAILED: {e}")
            dailies[code] = {}

    meta = {
        "us": {"name": "US Treasury", "short": "US",
               "source": "U.S. Department of the Treasury, daily par yield curve rates",
               "url": "https://home.treasury.gov/resource-center/data-chart-center/interest-rates",
               "note": "Par yields on recently auctioned Treasuries."},
        "ca": {"name": "Government of Canada", "short": "Canada",
               "source": "Bank of Canada Valet API, treasury bill and benchmark bond yields",
               "url": "https://www.bankofcanada.ca/valet/docs",
               "note": "Bills 1M to 1Y, benchmark bonds 2Y to 10Y, long bond shown at 30Y. No 20Y benchmark."},
        "eu": {"name": "Euro Area AAA", "short": "Euro area",
               "source": "ECB Data Portal, AAA-rated euro area government bond spot rates",
               "url": "https://data.ecb.europa.eu/data/datasets/YC",
               "note": "Fitted (Svensson) spot curve across AAA sovereigns, not a single country's bonds. No 1M point."},
    }

    all_months = set()
    monthly_maps = {}
    for code, daily in dailies.items():
        if not daily and prev.get("monthly", {}).get(code):
            # keep last good data if a source is down today
            print(f"  {code}: using previous data")
            pm = prev["months"]
            monthly_maps[code] = {m: c for m, c in zip(pm, prev["monthly"][code]) if c}
            meta[code]["stale"] = True
            meta[code]["latest"] = prev.get("meta", {}).get(code, {}).get("latest")
            continue
        monthly_maps[code] = month_end(daily)
        meta[code]["latest"] = max(daily) if daily else None
    for m in monthly_maps.values():
        all_months.update(m.keys())
    months = sorted(all_months)

    out = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tenors": TENORS,
        "months": months,
        "monthly": {c: [monthly_maps[c].get(m) for m in months] for c in fetchers},
        "daily": {},
        "meta": meta,
    }
    cutoff = (date.today() - timedelta(days=DAILY_KEEP_DAYS)).isoformat()
    for code, daily in dailies.items():
        if not daily:
            out["daily"][code] = prev.get("daily", {}).get(code, {"dates": [], "curves": []})
            continue
        ds = [d for d in sorted(daily) if d >= cutoff]
        out["daily"][code] = {"dates": ds, "curves": [daily[d] for d in ds]}

    ok = [c for c in fetchers if out["meta"][c].get("latest")]
    print("Latest:", {c: out["meta"][c].get("latest") for c in fetchers})
    if not ok:
        print("No data from any source; not writing.")
        sys.exit(1)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"Wrote {OUT} ({os.path.getsize(OUT)//1024} KB, {len(months)} months)")


if __name__ == "__main__":
    main()
