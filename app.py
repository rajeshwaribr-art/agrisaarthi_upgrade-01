"""AGRISAARTHI backend: Flask API + static front end."""
import math
import os
from datetime import datetime

from flask import Flask, g, jsonify, request, send_from_directory

import db
import geo
import model

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=os.path.join(BASE_DIR, "static"), static_url_path="")

ROAD_FACTOR = 1.3                                       # road km ~ straight-line km x 1.3
TRANSPORT_RS_PER_QTL_KM = float(os.environ.get("TRANSPORT_RS_PER_QTL_KM", "0.25"))
MAX_CANDIDATES = 12
STALE_DAYS = 7                                          # drop mandis that stopped reporting
HISTORY_DAYS = 60

_forecast_cache = {}


def get_db():
    if "db" not in g:
        g.db = db.connect()
        db.init_db(g.db)
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def clean(v):
    """JSON-safe number."""
    if v is None:
        return None
    v = float(v)
    return None if math.isnan(v) or math.isinf(v) else v


def err(message, status=400):
    return jsonify({"error": message}), status


# ------------------------------------------------------------------ routes
@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/health")
def health():
    conn = get_db()
    row = conn.execute(
        "SELECT COUNT(*) AS n, MAX(date) AS last, "
        "SUM(source = 'demo') AS demo FROM prices").fetchone()
    return jsonify({
        "status": "ok", "price_rows": row["n"], "latest_date": row["last"],
        "demo_rows": row["demo"] or 0,
        "markets": conn.execute("SELECT COUNT(*) FROM markets").fetchone()[0],
        "crops": conn.execute("SELECT COUNT(*) FROM crops").fetchone()[0],
    })


@app.route("/api/crops")
def crops():
    rows = get_db().execute(
        "SELECT c.name FROM crops c WHERE EXISTS (SELECT 1 FROM prices p WHERE p.crop_id = c.id) "
        "ORDER BY c.name").fetchall()
    return jsonify({"crops": [r["name"] for r in rows]})


@app.route("/api/places")
def places():
    return jsonify({"places": geo.suggest(request.args.get("q", ""))})


def _get_forecast(market_id, crop_id, dates, prices):
    key = (market_id, crop_id, dates[-1], len(dates))
    if key not in _forecast_cache:
        if len(_forecast_cache) > 2000:
            _forecast_cache.clear()
        _forecast_cache[key] = model.forecast_series(dates, prices)
    return _forecast_cache[key]


def _advice(mode, best, nearest, as_of_note):
    parts = []
    area = best["district"] if best["district"] and best["district"] != best["market"] else best["state"]
    name = f"{best['market']} ({area})"
    if mode == "seller":
        if nearest["market_id"] == best["market_id"]:
            parts.append(f"{name} is both the nearest and the best-paying option after transport.")
        else:
            gain = best["net_price"] - nearest["net_price"]
            if gain > 0:
                parts.append(
                    f"{name} is estimated to leave you about ₹{gain:,.0f}/quintal more than the nearest mandi "
                    f"({nearest['market']}) after transport.")
            else:
                parts.append(f"{name} ranks first after transport.")
    else:
        if nearest["market_id"] == best["market_id"]:
            parts.append(f"{name} is both the nearest and the cheapest option once transport is included.")
        else:
            saving = nearest["net_price"] - best["net_price"]
            parts.append(
                f"Buying at {name} is estimated to cost about ₹{saving:,.0f}/quintal less than the nearest mandi "
                f"({nearest['market']}) including transport.")
    fc = best["forecast"]
    if fc and best["latest_modal"]:
        peak = max(fc, key=lambda p: p["modal"])
        low = min(fc, key=lambda p: p["modal"])
        change = (best["expected_price"] - best["latest_modal"]) / best["latest_modal"] * 100
        if best["method"] == "persistence":
            parts.append("Recent history gives no reliable trend signal, so the outlook is 'prices roughly unchanged'.")
        elif mode == "seller" and change >= 3:
            parts.append(f"Prices there are expected to firm up about {change:.0f}% this week (high around {peak['date']}); "
                         "waiting a few days could help if your produce keeps.")
        elif mode == "seller" and change <= -3:
            parts.append(f"Prices there are expected to ease about {abs(change):.0f}% this week; selling sooner is favoured.")
        elif mode == "buyer" and change <= -3:
            parts.append(f"Prices there are expected to ease about {abs(change):.0f}% this week (low around {low['date']}); "
                         "you may get a better rate by waiting.")
        elif mode == "buyer" and change >= 3:
            parts.append(f"Prices there are expected to rise about {change:.0f}% this week; buying sooner is favoured.")
        else:
            parts.append("Prices there look broadly stable over the next week.")
    parts.append("Forecasts are statistical estimates, not guarantees; confirm the rate with the mandi before you travel. "
                 + as_of_note)
    return " ".join(parts)


@app.route("/api/compare")
def compare():
    conn = get_db()
    mode = (request.args.get("mode") or "seller").lower()
    if mode not in ("seller", "buyer"):
        return err("mode must be 'seller' or 'buyer'.")

    crop_name = (request.args.get("crop") or "").strip()
    if not crop_name:
        return err("Please choose a crop.")
    crop = conn.execute("SELECT id, name FROM crops WHERE LOWER(name) = LOWER(?)", (crop_name,)).fetchone()
    if crop is None:
        return err(f"No price data for crop '{crop_name}'.", 404)

    try:
        radius = float(request.args.get("radius", 200))
        limit = int(request.args.get("limit", 6))
    except ValueError:
        return err("radius and limit must be numbers.")
    radius = min(max(radius, 10), 1000)
    limit = min(max(limit, 1), 10)

    try:
        lat_s, lon_s = request.args.get("lat"), request.args.get("lon")
        if lat_s not in (None, "") and lon_s not in (None, ""):
            origin = geo.from_coords(lat_s, lon_s)
        else:
            origin = geo.resolve_location(request.args.get("location"))
    except geo.LocationError as exc:
        return err(str(exc), 404)
    except ValueError:
        return err("lat and lon must be numbers.")

    cand_rows = conn.execute(
        """SELECT m.id, m.name, m.state, m.district, m.lat, m.lon, MAX(p.date) AS latest
           FROM prices p JOIN markets m ON m.id = p.market_id
           WHERE p.crop_id = ? AND m.lat IS NOT NULL AND m.lon IS NOT NULL
           GROUP BY m.id""", (crop["id"],)).fetchall()
    if not cand_rows:
        return err(f"No mandis with known locations have {crop['name']} prices yet.", 404)

    as_of = max(r["latest"] for r in cand_rows)
    as_of_dt = datetime.strptime(as_of, "%Y-%m-%d")
    fresh, stale = [], 0
    for r in cand_rows:
        if (as_of_dt - datetime.strptime(r["latest"], "%Y-%m-%d")).days > STALE_DAYS:
            stale += 1
            continue
        km = geo.haversine_km(origin["lat"], origin["lon"], r["lat"], r["lon"])
        fresh.append((km, r))
    fresh.sort(key=lambda t: t[0])
    if not fresh:
        return err("Price data for this crop is out of date.", 404)

    within = [t for t in fresh if t[0] <= radius]
    expanded = len(within) < 3
    candidates = (fresh[:5] if expanded else within)[:MAX_CANDIDATES]

    results = []
    for km, r in candidates:
        series = conn.execute(
            "SELECT date, modal_price, min_price, max_price, source FROM prices "
            "WHERE market_id = ? AND crop_id = ? ORDER BY date", (r["id"], crop["id"])).fetchall()
        dates = [s["date"] for s in series]
        prices = [s["modal_price"] for s in series]
        fc = _get_forecast(r["id"], crop["id"], dates, prices)
        latest = series[-1]
        road_km = km * ROAD_FACTOR
        transport = road_km * TRANSPORT_RS_PER_QTL_KM
        if fc["forecast"]:
            expected = sum(p["modal"] for p in fc["forecast"]) / len(fc["forecast"])
            basis = "forecast"
        else:
            expected, basis = latest["modal_price"], "latest"
        net = expected - transport if mode == "seller" else expected + transport
        cutoff = (as_of_dt.toordinal() - HISTORY_DAYS)
        hist = [{"date": s["date"], "modal": clean(s["modal_price"])} for s in series
                if datetime.strptime(s["date"], "%Y-%m-%d").toordinal() >= cutoff]
        results.append({
            "market_id": r["id"], "market": r["name"], "district": r["district"], "state": r["state"],
            "distance_km": round(km, 1), "road_km_est": round(road_km),
            "latest_date": latest["date"], "latest_modal": clean(latest["modal_price"]),
            "latest_min": clean(latest["min_price"]), "latest_max": clean(latest["max_price"]),
            "expected_price": round(expected, 1), "basis": basis,
            "transport_cost": round(transport, 1), "net_price": round(net, 1),
            "method": fc["method"], "mape": fc["mape"], "history_days": fc["n_points"],
            "source": latest["source"], "history": hist, "forecast": fc["forecast"],
            "within_radius": km <= radius,
        })

    results.sort(key=lambda x: x["net_price"], reverse=(mode == "seller"))
    top = results[:limit]
    for i, item in enumerate(top, 1):
        item["rank"] = i
    nearest = min(results, key=lambda x: x["distance_km"])
    sources = {x["source"] for x in top}
    data_source = "demo" if sources == {"demo"} else ("live" if "demo" not in sources else "mixed")
    note = f"Prices as of {as_of}."
    if data_source != "live":
        note += " Includes DEMO data, not real market prices."
    return jsonify({
        "location": {"label": origin["name"], "state": origin["state"], "lat": origin["lat"],
                     "lon": origin["lon"], "matched_by": origin["matched_by"]},
        "crop": crop["name"], "mode": mode, "radius_km": radius, "expanded_search": expanded,
        "as_of": as_of, "data_source": data_source, "excluded_stale": stale,
        "assumptions": {"road_factor": ROAD_FACTOR, "transport_rs_per_qtl_km": TRANSPORT_RS_PER_QTL_KM,
                        "unit": "Rs per quintal"},
        "results": top, "best": top[0],
        "advice": _advice(mode, top[0], nearest, note),
    })


# ------------------------------------------------------------------ harvest planning
@app.route("/api/harvest-advisor", methods=["POST"])
def harvest_advisor():
    """Return a transparent harvest planning estimate from farmer-supplied inputs.

    No crop calendar or live weather feed is bundled with this project. The app
    therefore never invents crop-specific maturity periods: users may supply
    the maturity duration from their seed/variety guidance.
    """
    payload = request.get_json(silent=True) or {}
    crop_name = str(payload.get("crop", "")).strip()
    state = str(payload.get("state", "")).strip()
    sowing_date = str(payload.get("sowing_date", "")).strip()
    days_raw = payload.get("maturity_days")
    if not crop_name or not state or not sowing_date:
        return err("Please choose a crop and state and enter the sowing date.")
    crop = get_db().execute(
        "SELECT name FROM crops WHERE LOWER(name) = LOWER(?)", (crop_name,)
    ).fetchone()
    if crop is None:
        return err("This crop is not present in the application's crop database. Refresh the crop list.", 404)
    try:
        sowed = datetime.strptime(sowing_date, "%Y-%m-%d").date()
    except ValueError:
        return err("Please enter a valid sowing date in YYYY-MM-DD format.")
    maturity_date = None
    if days_raw not in (None, ""):
        try:
            days = int(days_raw)
        except (TypeError, ValueError):
            return err("Maturity period must be a whole number of days.")
        if not 1 <= days <= 500:
            return err("Maturity period must be between 1 and 500 days.")
        from datetime import timedelta
        maturity_date = (sowed + timedelta(days=days)).isoformat()
    return jsonify({
        "crop": crop["name"], "state": state, "sowing_date": sowing_date,
        "maturity_days": int(days_raw) if days_raw not in (None, "") else None,
        "estimated_maturity_date": maturity_date,
        "estimate_basis": "farmer_supplied_variety_duration" if maturity_date else "duration_not_provided",
        "readiness_checks": [
            "Check the crop-specific maturity signs for your variety (grain/fruit size, colour, firmness or dry-down as applicable).",
            "Inspect several representative plants across the field, not just the field edge.",
            "Check soil moisture and the short-term local weather before harvesting; avoid harvesting wet produce where drying is required.",
            "Confirm the recommended harvest stage and post-harvest handling with your local Krishi Vigyan Kendra or agriculture officer."
        ],
        "notice": "This is a planning estimate, not a crop-specific official calendar. Actual maturity varies with variety, season, sowing method and local conditions. No live weather data is used."
    })


if __name__ == "__main__":
    conn = db.connect()
    db.init_db(conn)
    empty = conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0] == 0
    conn.close()
    if empty:
        print("Database is empty. Run:  python collector.py --seed-demo   (or --live / --csv)")
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)), debug=False)
