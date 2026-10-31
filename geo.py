"""Gazetteer, location resolution and distance maths for AGRISAARTHI.

Coordinates are approximate town / district-HQ centres (good to a few km),
which is accurate enough for ranking mandis by distance. Agmarknet itself does
not publish mandi coordinates, so real mandis are matched to this list by name.
"""
import difflib
import math
import re

# (name, state, lat, lon, comma-separated aliases)
PLACES = [
    # Karnataka
    ("Bengaluru", "Karnataka", 12.9716, 77.5946, "bangalore,bengaluru urban,bangalore urban,yeshwanthpur,yeshwantpur"),
    ("Kolar", "Karnataka", 13.1362, 78.1291, ""),
    ("Chikkaballapur", "Karnataka", 13.4355, 77.7315, "chikballapur,chickballapur"),
    ("Ramanagara", "Karnataka", 12.7159, 77.2815, "ramanagaram"),
    ("Tumakuru", "Karnataka", 13.3392, 77.1140, "tumkur"),
    ("Mandya", "Karnataka", 12.5218, 76.8951, ""),
    ("Mysuru", "Karnataka", 12.2958, 76.6394, "mysore"),
    ("Hassan", "Karnataka", 13.0068, 76.1004, ""),
    ("Davanagere", "Karnataka", 14.4644, 75.9218, "davangere"),
    ("Hubballi", "Karnataka", 15.3647, 75.1240, "hubli,dharwad"),
    ("Belagavi", "Karnataka", 15.8497, 74.4977, "belgaum"),
    ("Ballari", "Karnataka", 15.1394, 76.9214, "bellary"),
    ("Shivamogga", "Karnataka", 13.9299, 75.5681, "shimoga"),
    ("Chitradurga", "Karnataka", 14.2251, 76.3980, ""),
    ("Kalaburagi", "Karnataka", 17.3297, 76.8343, "gulbarga"),
    ("Vijayapura", "Karnataka", 16.8302, 75.7100, "bijapur"),
    ("Mangaluru", "Karnataka", 12.9141, 74.8560, "mangalore"),
    ("Chikkamagaluru", "Karnataka", 13.3161, 75.7720, "chikmagalur"),
    ("Chamarajanagar", "Karnataka", 11.9261, 76.9437, "chamarajnagar"),
    ("Bidar", "Karnataka", 17.9104, 77.5199, ""),
    # Tamil Nadu
    ("Hosur", "Tamil Nadu", 12.7409, 77.8253, ""),
    ("Krishnagiri", "Tamil Nadu", 12.5266, 78.2150, ""),
    ("Dharmapuri", "Tamil Nadu", 12.1211, 78.1582, ""),
    ("Salem", "Tamil Nadu", 11.6643, 78.1460, ""),
    ("Coimbatore", "Tamil Nadu", 11.0168, 76.9558, "koyambedu coimbatore"),
    ("Chennai", "Tamil Nadu", 13.0827, 80.2707, "koyambedu,madras"),
    ("Vellore", "Tamil Nadu", 12.9165, 79.1325, ""),
    ("Erode", "Tamil Nadu", 11.3410, 77.7172, ""),
    ("Madurai", "Tamil Nadu", 9.9252, 78.1198, ""),
    ("Tiruchirappalli", "Tamil Nadu", 10.7905, 78.7047, "trichy,tiruchirapalli"),
    ("Dindigul", "Tamil Nadu", 10.3673, 77.9803, ""),
    # Andhra Pradesh
    ("Chittoor", "Andhra Pradesh", 13.2172, 79.1003, ""),
    ("Madanapalle", "Andhra Pradesh", 13.5503, 78.5030, "madanapalli"),
    ("Hindupur", "Andhra Pradesh", 13.8281, 77.4910, ""),
    ("Anantapur", "Andhra Pradesh", 14.6819, 77.6006, "anantapuramu,ananthapur"),
    ("Kurnool", "Andhra Pradesh", 15.8281, 78.0373, ""),
    ("Tirupati", "Andhra Pradesh", 13.6288, 79.4192, ""),
    ("Vijayawada", "Andhra Pradesh", 16.5062, 80.6480, ""),
    ("Guntur", "Andhra Pradesh", 16.3067, 80.4365, ""),
    ("Visakhapatnam", "Andhra Pradesh", 17.6868, 83.2185, "vizag"),
    # Telangana
    ("Hyderabad", "Telangana", 17.3850, 78.4867, "bowenpally,secunderabad"),
    ("Warangal", "Telangana", 17.9689, 79.5941, ""),
    ("Nizamabad", "Telangana", 18.6725, 78.0941, ""),
    # Kerala
    ("Kochi", "Kerala", 9.9312, 76.2673, "cochin,ernakulam"),
    ("Thiruvananthapuram", "Kerala", 8.5241, 76.9366, "trivandrum"),
    ("Kozhikode", "Kerala", 11.2588, 75.7804, "calicut"),
    ("Palakkad", "Kerala", 10.7867, 76.6548, "palghat"),
    # Maharashtra
    ("Pune", "Maharashtra", 18.5204, 73.8567, ""),
    ("Nashik", "Maharashtra", 19.9975, 73.7898, "nasik"),
    ("Lasalgaon", "Maharashtra", 20.1400, 74.2300, ""),
    ("Mumbai", "Maharashtra", 19.0760, 72.8777, "vashi,navi mumbai"),
    ("Nagpur", "Maharashtra", 21.1458, 79.0882, ""),
    ("Solapur", "Maharashtra", 17.6599, 75.9064, ""),
    ("Kolhapur", "Maharashtra", 16.7050, 74.2433, ""),
    # Gujarat
    ("Ahmedabad", "Gujarat", 23.0225, 72.5714, ""),
    ("Rajkot", "Gujarat", 22.3039, 70.8022, ""),
    ("Surat", "Gujarat", 21.1702, 72.8311, ""),
    # Madhya Pradesh
    ("Indore", "Madhya Pradesh", 22.7196, 75.8577, ""),
    ("Bhopal", "Madhya Pradesh", 23.2599, 77.4126, ""),
    # Uttar Pradesh
    ("Lucknow", "Uttar Pradesh", 26.8467, 80.9462, ""),
    ("Kanpur", "Uttar Pradesh", 26.4499, 80.3319, ""),
    ("Agra", "Uttar Pradesh", 27.1767, 78.0081, ""),
    # North / East
    ("Delhi", "NCT of Delhi", 28.6139, 77.2090, "azadpur,new delhi"),
    ("Jaipur", "Rajasthan", 26.9124, 75.7873, ""),
    ("Ludhiana", "Punjab", 30.9010, 75.8573, ""),
    ("Karnal", "Haryana", 29.6857, 76.9905, ""),
    ("Kolkata", "West Bengal", 22.5726, 88.3639, "calcutta"),
    ("Bhubaneswar", "Odisha", 20.2961, 85.8245, ""),
    ("Patna", "Bihar", 25.5941, 85.1376, ""),
]

EARTH_RADIUS_KM = 6371.0088


def norm(text):
    """Lower-case, drop 'apmc' and punctuation, collapse spaces."""
    t = re.sub(r"\bapmc\b", " ", str(text or "").lower())
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return t.strip()


def _build_index():
    idx = {}
    for name, state, lat, lon, aliases in PLACES:
        place = {"name": name, "state": state, "lat": lat, "lon": lon}
        idx[norm(name)] = place
        for a in aliases.split(","):
            a = norm(a)
            if a and a not in idx:
                idx[a] = place
    return idx


_INDEX = _build_index()
_KEYS = sorted(_INDEX)


def haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


_COORD_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*[,\s]\s*(-?\d+(?:\.\d+)?)\s*$")


class LocationError(ValueError):
    pass


def in_india_box(lat, lon):
    return 6.0 <= lat <= 37.5 and 68.0 <= lon <= 97.5


def from_coords(lat, lon):
    lat, lon = float(lat), float(lon)
    if not in_india_box(lat, lon):
        raise LocationError("Those coordinates look to be outside India.")
    return {"name": f"{lat:.4f}, {lon:.4f}", "state": None, "lat": lat, "lon": lon, "matched_by": "coordinates"}


def resolve_location(query):
    """Turn free text ('Kolar', 'bangalore, karnataka', '12.97, 77.59') into a place."""
    q = (query or "").strip()
    if not q:
        raise LocationError("Please enter a location.")
    m = _COORD_RE.match(q)
    if m:
        return from_coords(m.group(1), m.group(2))
    parts = [norm(p) for p in q.split(",") if norm(p)]
    if not parts:
        raise LocationError("Please enter a location.")
    for n in parts:
        if n in _INDEX:
            return dict(_INDEX[n], matched_by="name")
    n = parts[0]
    if len(n) >= 3:
        pref = [k for k in _KEYS if k.startswith(n)]
        if pref:
            return dict(_INDEX[min(pref, key=len)], matched_by="prefix")
        close = difflib.get_close_matches(n, _KEYS, n=1, cutoff=0.8)
        if close:
            return dict(_INDEX[close[0]], matched_by="fuzzy")
    raise LocationError(
        f"Couldn't find '{q}'. Try a nearby town or district (e.g. Kolar, Hosur), "
        "or use the 'Use my location' button."
    )


def suggest(query, limit=8):
    n = norm(query)
    if not n:
        return []
    seen, out = set(), []
    for k in _KEYS:
        if k.startswith(n) or (len(n) >= 3 and n in k):
            p = _INDEX[k]
            if p["name"] not in seen:
                seen.add(p["name"])
                out.append({"name": p["name"], "state": p["state"]})
    out.sort(key=lambda d: (not norm(d["name"]).startswith(n), d["name"]))
    return out[:limit]


def _state_ok(state, place_state):
    a, b = norm(state), norm(place_state)
    if not a or not b:
        return True
    return a == b or a in b or b in a


def place_for_market(market, district, state):
    """Best-effort coordinates for a real Agmarknet market. Returns place dict or None."""
    for candidate in (market, district):
        n = norm(candidate)
        if not n:
            continue
        p = _INDEX.get(n)
        if p is None:
            # e.g. "Hosur(Krishnagiri)" -> try the first word group
            p = _INDEX.get(n.split(" ")[0]) if " " in n else None
        if p is not None and _state_ok(state, p["state"]):
            return p
    return None
