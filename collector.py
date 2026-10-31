"""Data collection for AGRISAARTHI.

Sources (all end up in the same SQLite tables):
  --live        Agmarknet daily prices through the official data.gov.in API
                (the dataset is generated from agmarknet.gov.in). Needs a free
                API key in the DATA_GOV_IN_API_KEY environment variable.
                The API returns the current day, so run this daily to build history.
  --csv FILE    Import a price export (Agmarknet report / CEDA download) to
                load history in one go.
  --seed-demo   Generate clearly-labelled DEMO prices so the app can be tried
                without network access. Demo rows are marked source='demo'.

Examples:
    python collector.py --seed-demo
    DATA_GOV_IN_API_KEY=xxxx python collector.py --live --purge-demo
    python collector.py --csv my_agmarknet_history.csv
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta

import numpy as np
import pandas as pd

import db
import geo

RESOURCE_ID = "9ef84268-d588-465a-a308-a864a43d0070"   # Variety-wise daily market prices (Agmarknet)
API_URL = f"https://api.data.gov.in/resource/{RESOURCE_ID}"

DEMO_CROPS = {            # base modal price, Rs/quintal, seasonal amplitude, seasonal peak day-of-year
    "Tomato": (1800, 0.28, 150),
    "Onion": (1600, 0.20, 300),
    "Potato": (1500, 0.12, 60),
    "Maize": (2100, 0.06, 240),
    "Wheat": (2500, 0.04, 120),
    "Rice": (3000, 0.05, 330),
}


# --------------------------------------------------------------------------- ingest
def _canon(col):
    c = str(col).lower().replace("x0020", "")
    return re.sub(r"[^a-z0-9]", "", c)


def normalise_frame(df):
    """Map assorted column spellings to state/district/market/commodity/date/prices."""
    df = df.rename(columns={c: _canon(c) for c in df.columns})
    alias = {"arrivaldate": "date", "pricedate": "date", "reporteddate": "date",
             "minprice": "min_price", "maxprice": "max_price", "modalprice": "modal_price",
             "marketname": "market", "commodityname": "commodity"}
    df = df.rename(columns=alias)
    need = ["state", "market", "commodity", "date", "modal_price"]
    missing = [c for c in need if c not in df.columns]
    if missing:
        raise ValueError(f"Missing columns {missing}; found {list(df.columns)}")
    for c in ("district", "min_price", "max_price"):
        if c not in df.columns:
            df[c] = np.nan
    df["date"] = pd.to_datetime(df["date"], dayfirst=True, errors="coerce")
    for c in ("min_price", "max_price", "modal_price"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["date", "modal_price", "state", "market", "commodity"])
    df = df[df["modal_price"] > 0]
    for c in ("state", "district", "market", "commodity"):
        df[c] = df[c].astype("string").str.strip()
    df["min_price"] = df["min_price"].fillna(df["modal_price"])
    df["max_price"] = df["max_price"].fillna(df["modal_price"])
    return df


def ingest(conn, df, source):
    """Aggregate varieties per market/crop/day and store. Returns stats."""
    df = normalise_frame(df)
    grp = (df.groupby(["state", "district", "market", "commodity", "date"], dropna=False)
             [["min_price", "max_price", "modal_price"]].mean().reset_index())
    stats = {"rows": 0, "markets": 0, "unlocated_markets": set()}
    seen_markets = set()
    market_ids, crop_ids = {}, {}
    rows = []
    for r in grp.itertuples(index=False):
        district = None if pd.isna(r.district) else str(r.district)
        mkey = (str(r.market), str(r.state))
        if mkey not in market_ids:
            place = geo.place_for_market(r.market, district, r.state)
            lat, lon = (place["lat"], place["lon"]) if place else (None, None)
            market_ids[mkey] = db.upsert_market(conn, str(r.market), str(r.state), district, lat, lon)
            if place is None:
                stats["unlocated_markets"].add(f"{r.market} ({r.state})")
            seen_markets.add(mkey)
        crop = str(r.commodity)
        if crop not in crop_ids:
            crop_ids[crop] = db.upsert_crop(conn, crop)
        rows.append((market_ids[mkey], crop_ids[crop], r.date.strftime("%Y-%m-%d"),
                     float(r.min_price), float(r.max_price), float(r.modal_price), source))
    conn.executemany(
        "INSERT OR REPLACE INTO prices (market_id, crop_id, date, min_price, max_price, modal_price, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    stats["rows"], stats["markets"] = len(rows), len(seen_markets)
    return stats


def import_csv(conn, path):
    return ingest(conn, pd.read_csv(path), "agmarknet")


# --------------------------------------------------------------------------- live fetch
def fetch_live(api_key, states=None, commodities=None, page_size=500, max_pages=80, pause=0.5):
    """Download records from the data.gov.in Agmarknet resource and return a DataFrame."""
    records = []
    combos = [(s, c) for s in (states or [None]) for c in (commodities or [None])]
    for state, commodity in combos:
        offset = 0
        for _ in range(max_pages):
            params = {"api-key": api_key, "format": "json", "limit": page_size, "offset": offset}
            if state:
                params["filters[state.keyword]"] = state
            if commodity:
                params["filters[commodity]"] = commodity
            url = API_URL + "?" + urllib.parse.urlencode(params)
            payload = _get_json(url)
            batch = payload.get("records") or []
            records.extend(batch)
            offset += len(batch)
            total = int(payload.get("total") or 0)
            if not batch or offset >= total:
                break
            time.sleep(pause)
    return pd.DataFrame.from_records(records)


def _get_json(url, tries=3):
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "agrisaarthi/1.0", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Could not fetch {url.split('?')[0]}: {last}")


def purge_demo(conn):
    n = conn.execute("DELETE FROM prices WHERE source = 'demo'").rowcount
    conn.execute("DELETE FROM markets WHERE id NOT IN (SELECT DISTINCT market_id FROM prices)")
    conn.execute("DELETE FROM crops WHERE id NOT IN (SELECT DISTINCT crop_id FROM prices)")
    conn.commit()
    return n


# --------------------------------------------------------------------------- demo data
def seed_demo(conn, days=420, end=None, seed=7):
    """Synthetic prices (NOT real). Seasonal + trending random walk per market and crop."""
    rng = np.random.default_rng(seed)
    end = end or date.today()
    dates = [end - timedelta(days=i) for i in range(days - 1, -1, -1)]
    doy = np.array([d.timetuple().tm_yday for d in dates])
    crop_ids = {name: db.upsert_crop(conn, name) for name in DEMO_CROPS}
    rows = []
    n_markets = 0
    for name, state, lat, lon, _ in geo.PLACES:
        mid = db.upsert_market(conn, name, state, name, lat, lon)
        n_markets += 1
        for crop, (base, amp, peak) in DEMO_CROPS.items():
            if rng.random() < 0.15:          # not every mandi trades every crop
                continue
            factor = float(np.clip(rng.normal(1.0, 0.06), 0.85, 1.2))
            ar, level = 0.0, []
            for _ in range(days):
                ar = 0.95 * ar + rng.normal(0, 0.018)
                level.append(ar)
            seasonal = 1 + amp * np.cos(2 * np.pi * (doy - peak) / 365.25)
            modal = base * factor * seasonal * np.exp(np.array(level)) * np.exp(rng.normal(0, 0.008, days))
            keep = rng.random(days) > 0.08   # occasional non-reporting days
            for d, m, k in zip(dates, modal, keep):
                if not k:
                    continue
                m = round(float(m) / 10) * 10
                rows.append((mid, crop_ids[crop], d.isoformat(), round(m * 0.88 / 10) * 10,
                             round(m * 1.12 / 10) * 10, m, "demo"))
    conn.executemany(
        "INSERT OR REPLACE INTO prices (market_id, crop_id, date, min_price, max_price, modal_price, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    return {"rows": len(rows), "markets": n_markets, "crops": len(DEMO_CROPS)}


# --------------------------------------------------------------------------- CLI
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed-demo", action="store_true", help="generate demo data (not real prices)")
    ap.add_argument("--live", action="store_true", help="fetch today's prices from data.gov.in (Agmarknet)")
    ap.add_argument("--csv", metavar="FILE", help="import an Agmarknet/CEDA CSV export")
    ap.add_argument("--states", help="comma-separated state filter for --live (default: all)")
    ap.add_argument("--commodities", help="comma-separated commodity filter for --live (default: all)")
    ap.add_argument("--purge-demo", action="store_true", help="delete demo rows (use once real data is loaded)")
    args = ap.parse_args(argv)
    if not (args.seed_demo or args.live or args.csv or args.purge_demo):
        ap.print_help()
        return 1

    conn = db.connect()
    db.init_db(conn)
    if args.seed_demo:
        print("Demo data:", seed_demo(conn))
    if args.csv:
        s = import_csv(conn, args.csv)
        print(f"CSV import: {s['rows']} rows, {s['markets']} markets")
        _report_unlocated(s)
    if args.live:
        key = os.environ.get("DATA_GOV_IN_API_KEY")
        if not key:
            print("Set DATA_GOV_IN_API_KEY (free key from https://data.gov.in) first.", file=sys.stderr)
            return 2
        split = lambda v: [x.strip() for x in v.split(",") if x.strip()] if v else None
        try:
            df = fetch_live(key, split(args.states), split(args.commodities))
        except RuntimeError as exc:
            print(f"Live fetch failed: {exc}", file=sys.stderr)
            return 3
        if df.empty:
            print("Live fetch returned no records (check the key / filters).", file=sys.stderr)
            return 3
        s = ingest(conn, df, "agmarknet")
        print(f"Live fetch: {s['rows']} rows, {s['markets']} markets")
        _report_unlocated(s)
    if args.purge_demo:
        print("Removed demo rows:", purge_demo(conn))
    conn.close()
    return 0


def _report_unlocated(stats):
    miss = sorted(stats["unlocated_markets"])
    if miss:
        print(f"{len(miss)} markets stored without coordinates (excluded from nearby search until "
              f"added to geo.PLACES), e.g.: {', '.join(miss[:5])}")


if __name__ == "__main__":
    sys.exit(main())
