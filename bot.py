import os
import re
import csv
import requests
import threading
from datetime import datetime, date, timedelta
import time
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
TEAMS_WEBHOOK = os.environ.get("TEAMS_WEBHOOK")

# --- PBN (Irish Revenue Customs RoRo) ---
# Revenue's public look-up is a form POST (with CSRF token) returning HTML:
#   POST https://www.ros.ie/customs-roro-control-web/ros/freight/lookup
#   pbnID=YL22UY97&registrationNumber=&arrivalDate=16-07-2026&arrivalKey=&pbnLookUp=true&_csrf=...
# The bot GETs the page for a CSRF token + session cookie, then POSTs the form.
# ROS_PBN_URL can be overridden (e.g. to a proxy); a non-ros.ie URL uses generic GET/POST JSON mode.
ROS_PBN_URL = os.environ.get("ROS_PBN_URL", "https://www.ros.ie/customs-roro-control-web/ros/freight/lookup")
ROS_PBN_METHOD = os.environ.get("ROS_PBN_METHOD", "GET").upper()
ROS_PBN_BODY = os.environ.get("ROS_PBN_BODY")
PBN_LOOKUP_PAGE = "https://www.ros.ie/customs-roro-control-web/ros/freight/lookup"

# =====================================================================
# MOVE IT API SETTINGS - all set in Railway Variables, never in code
#   MOVEIT_BASE_URL   e.g. https://yourserver.moveit.ie/api/v1
#   MOVEIT_API_KEY    the key the vendor issued
#   MOVEIT_API_PASSWORD
#   MOVEIT_AUTH_MODE  one of: basic | headers  (default: basic)
#     basic   = HTTP Basic auth, key as username, password as password
#     headers = sent as X-Api-Key / X-Api-Password request headers
# If MOVEIT_BASE_URL is not set, the bot runs exactly like v3 (manual flow).
# =====================================================================
MOVEIT_BASE_URL = (os.environ.get("MOVEIT_BASE_URL") or "").rstrip("/")
MOVEIT_API_KEY = os.environ.get("MOVEIT_API_KEY", "")
MOVEIT_API_PASSWORD = os.environ.get("MOVEIT_API_PASSWORD", "")
MOVEIT_AUTH_MODE = os.environ.get("MOVEIT_AUTH_MODE", "basic").lower()


class MoveItAPI:
    def __init__(self):
        self.enabled = bool(MOVEIT_BASE_URL and MOVEIT_API_KEY)
        self.session = requests.Session()
        if MOVEIT_AUTH_MODE == "headers":
            self.session.headers.update({
                "X-Api-Key": MOVEIT_API_KEY,
                "X-Api-Password": MOVEIT_API_PASSWORD,
            })
        else:
            self.session.auth = (MOVEIT_API_KEY, MOVEIT_API_PASSWORD)
        self.session.headers.update({"Accept": "application/json"})

    def _get(self, path, params=None, timeout=10, quiet=False):
        if not self.enabled:
            return None
        try:
            r = self.session.get(f"{MOVEIT_BASE_URL}/{path.lstrip('/')}", params=params, timeout=timeout)
            if not quiet:
                print(f"MoveIT API {path} -> {r.status_code}: {r.text[:300]}")
            if r.status_code == 200:
                return r.json()
        except Exception as e:
            print(f"MoveIT API error on {path}: {e}")
        return None

    def search_trailer(self, term):
        data = self._get("trailer", params={"searchterm": term})
        if isinstance(data, list) and data:
            return data[0]
        if isinstance(data, dict):
            return data
        return None

    def todays_jobs(self):
        today = date.today().strftime("%Y-%m-%d")
        data = self._get("job", params={"fromdate": today, "todate": today})
        return data if isinstance(data, list) else []

    def job_history(self, days=90, chunk_days=7):
        """Pull job history in small windows. Move IT's job endpoint 500s
        ('DBNull to Date') if any job in the requested range has a null date,
        so failed chunks are retried day-by-day and only the poisoned days skipped."""
        all_jobs, seen_ids = [], set()
        bad_days = 0
        end = date.today()
        cur = end - timedelta(days=days)

        def _collect(data):
            nonlocal all_jobs
            for j in data:
                jid = _find_key(j, "id", "jobid")
                if jid is None or jid not in seen_ids:
                    if jid is not None:
                        seen_ids.add(jid)
                    all_jobs.append(j)

        while cur <= end:
            chunk_end = min(cur + timedelta(days=chunk_days - 1), end)
            data = self._get("job", params={
                "fromdate": cur.strftime("%Y-%m-%d"),
                "todate": chunk_end.strftime("%Y-%m-%d"),
            }, timeout=30, quiet=True)
            if isinstance(data, list):
                _collect(data)
            else:
                # Chunk failed (likely a null-date job) - retry each day, skip the bad ones.
                d = cur
                while d <= chunk_end:
                    ds = d.strftime("%Y-%m-%d")
                    daily = self._get("job", params={"fromdate": ds, "todate": ds}, timeout=30, quiet=True)
                    if isinstance(daily, list):
                        _collect(daily)
                    else:
                        bad_days += 1
                    d += timedelta(days=1)
            cur = chunk_end + timedelta(days=1)

        print(f"[job-history] pulled {len(all_jobs)} jobs over {days} days"
              + (f"; {bad_days} day(s) unserveable (Move IT null-date records)" if bad_days else ""))
        return all_jobs

    def ferry_ports(self):
        data = self._get("ferryport")
        return data if isinstance(data, list) else []


API = MoveItAPI()


def _norm(s):
    return "".join(ch for ch in str(s).lower() if ch.isalnum())


def _find_key(obj, *candidates):
    """Case/format-insensitive field lookup - 'Job #', 'jobId', 'job_id' all match."""
    if not isinstance(obj, dict):
        return None
    normed = {_norm(k): v for k, v in obj.items()}
    for c in candidates:
        if _norm(c) in normed:
            return normed[_norm(c)]
    return None


def _as_text(value):
    if value is None:
        return None
    if isinstance(value, dict):
        return _find_key(value, "name", "code", "text", "description") or str(value)
    return str(value)


def md(value):
    """Escape Telegram Markdown-v1 special characters in dynamic text.
    An unpaired _ * ` or [ in a filename or address makes the whole message
    unsendable (400 'can't parse entities'), which kills the handler mid-conversation."""
    return (str(value)
            .replace("_", "\\_")
            .replace("*", "\\*")
            .replace("`", "\\`")
            .replace("[", "\\["))


def _stop_action_text(stop):
    return _norm(_as_text(_find_key(stop, "stopaction", "action")) or "")


def _stop_trailer_ref(stop):
    """A stop may carry its trailer reference under several possible keys."""
    t = _find_key(stop, "trailer", "starttrailer", "finishtrailer", "trailercode")
    if t is None:
        return None
    if isinstance(t, dict):
        return str(_find_key(t, "code", "registration") or ""), _find_key(t, "id")
    return str(t), None


def extract_job_summary(job):
    """Pull the useful fields out of a job record by walking its stops."""
    stops = _find_key(job, "stops") or []
    stops = sorted(stops, key=lambda s: _find_key(s, "stopdatetime") or "")

    collection_stop = next((s for s in stops if "collect" in _stop_action_text(s)), None)
    # "Drop Trailer" / "Deliver" style actions mark the delivery stop -
    # exclude any "collect" match so we don't double count.
    delivery_stop = next(
        (s for s in stops if ("drop" in _stop_action_text(s) or "deliver" in _stop_action_text(s))
         and "collect" not in _stop_action_text(s)),
        None,
    )

    return {
        "job_id": _find_key(job, "id", "jobid", "job #", "job_no", "jobnumber"),
        "customer": _as_text(_find_key(job, "customer", "customer name", "client")),
        "collection": _as_text(_find_key(collection_stop, "address")) if collection_stop else None,
        "delivery": _as_text(_find_key(delivery_stop, "address")) if delivery_stop else None,
        "collection_time": _find_key(collection_stop, "stopdatetime") if collection_stop else None,
        "delivery_time": _find_key(delivery_stop, "stopdatetime") if delivery_stop else None,
        "from_c": _as_text(_find_key(job, "from", "fromcountry", "origin")),
        "to_c": _as_text(_find_key(job, "to", "tocountry", "destination")),
        "ferry": _as_text(_find_key(job, "ferrybooking", "ferry", "crossing")),
    }


def job_matches_trailer(job, trailer_code, trailer_id):
    """Does this job reference our trailer? Checks the job itself, then walks its stops."""
    trailer_code = (trailer_code or "").upper()

    def _matches(code, tid):
        return (code or "").upper() == trailer_code or (trailer_id is not None and tid == trailer_id)

    # Some job payloads may still carry the trailer at top level - keep this as a fallback.
    t = _find_key(job, "trailer", "starttrailer", "finishtrailer", "trailercode")
    if t is not None:
        if isinstance(t, dict):
            if _matches(_find_key(t, "code", "registration"), _find_key(t, "id")):
                return True
        elif trailer_code in str(t).upper():
            return True

    # Per the live API, the trailer reference actually lives inside each stop.
    for stop in _find_key(job, "stops") or []:
        code, tid = _stop_trailer_ref(stop) or (None, None)
        if code is not None and _matches(code, tid):
            return True

    return False


FALLBACK_QUICK_POINTS = {
    "ABP Cahir":            ("Cahir", "Ireland", 52.3775, -7.9268),
    "G's Fresh Barway":     ("Barway, Ely", "UK", 52.3480, 0.2735),
    "Florette Lichfield":   ("Lichfield", "UK", 52.6816, -1.8317),
    "Delanchy Boulogne":    ("Boulogne-sur-Mer", "France", 50.7264, 1.6147),
    "La Boulangere":        ("Mortagne-sur-Sevre", "France", 46.9922, -0.9469),
    "Alpro Wevelgem":       ("Wevelgem", "Belgium", 50.8050, 3.1740),
    "Clarebout Mouscron":   ("Mouscron", "Belgium", 50.7439, 3.2062),
    "RDV Marck":            ("Marck", "France", 50.9490, 1.9520),
}

QUICK_POINTS = dict(FALLBACK_QUICK_POINTS)

ADDRESS_BOOK = []

def load_address_book():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locations.csv")
    if not os.path.exists(path):
        print("No locations.csv found - running with quick buttons only")
        return
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            with open(path, newline="", encoding=enc) as f:
                rows = list(csv.DictReader(f))
            for row in rows:
                name = (row.get("Name") or "").strip()
                if name:
                    ADDRESS_BOOK.append({
                        "name": name,
                        "city": (row.get("City") or "").strip(),
                        "country": (row.get("Country") or "").strip(),
                    })
            print(f"Loaded {len(ADDRESS_BOOK)} locations from locations.csv (encoding: {enc})")
            return
        except UnicodeDecodeError:
            continue
        except Exception as e:
            print(f"Could not load locations.csv: {e}")
            return
    print("Could not load locations.csv: unknown text encoding")


def search_locations(query, limit=8):
    q = query.lower().strip()
    if len(q) < 2:
        return []
    starts, contains = [], []
    for loc in ADDRESS_BOOK:
        n = loc["name"].lower()
        if n.startswith(q):
            starts.append(loc)
        elif q in n or q in loc["city"].lower():
            contains.append(loc)
        if len(starts) >= limit:
            break
    return (starts + contains)[:limit]


FALLBACK_PORTS = {
    "Dublin Port":  (53.3456, -6.2045),
    "Rosslare":     (52.2519, -6.3387),
    "Holyhead":     (53.3090, -4.6329),
    "Liverpool":    (53.4515, -3.0189),
    "Hull":         (53.7446, -0.3352),
    "Eurotunnel":   (50.9360, 1.8130),
    "Folkestone":   (51.0930, 1.1210),
    "Dover":        (51.1279, 1.3134),
    "Calais":       (50.9513, 1.8587),
    "Cherbourg":    (49.6337, -1.6222),
    "Cairnryan":    (54.9713, -5.0245),
    "Larne":        (54.8517, -5.8060),
    "Belfast":      (54.6079, -5.9264),
}

PORTS = dict(FALLBACK_PORTS)

EU_COUNTRIES = {"ireland", "france", "germany", "netherlands", "belgium", "spain", "italy", "poland"}
NI_PORTS = {"Larne", "Belfast"}
DIRECT_EU_PORTS = {"Cherbourg"}
# GB <-> Ireland RoRo routes where Revenue's PBN applies
_IRISH_SEA_KEYWORDS = {"Dublin", "Rosslare", "Holyhead", "Liverpool", "Fishguard", "Pembroke", "Heysham"}
IRISH_SEA_PORTS = set(_IRISH_SEA_KEYWORDS)
COUNTRY_OPTIONS = ["UK", "Northern Ireland", "Ireland", "France", "Germany",
                   "Netherlands", "Belgium", "Spain", "Italy", "Poland", "Other"]


def _resolve_named_ports(keywords):
    """Map known keyword ports (e.g. 'Belfast') to whatever name the live API actually returned,
    so customs logic keeps working even if Move IT's naming differs slightly (e.g. 'Belfast Terminal')."""
    resolved = set()
    for kw in keywords:
        match = next((p for p in PORTS if kw.lower() in p.lower()), None)
        resolved.add(match or kw)
    return resolved


def load_ports_from_api():
    """Populate PORTS from Move IT's ferryport endpoint; falls back to the static list on any failure."""
    global PORTS, NI_PORTS, DIRECT_EU_PORTS
    if not API.enabled:
        print("Move IT API disabled - using fallback ports list")
        return
    data = API.ferry_ports()
    if not data:
        print("No ferry ports returned from Move IT - using fallback ports list")
        return

    loaded = {}
    for p in data:
        addr = _find_key(p, "address")
        addr = addr if isinstance(addr, dict) else {}
        # Per the live API, the friendly name and coordinates live inside address;
        # fall back to top-level fields just in case.
        name = (_as_text(_find_key(addr, "name")) or _as_text(_find_key(p, "name", "portname", "code")) or "").strip()
        lat = _find_key(addr, "lat", "latitude")
        lon = _find_key(addr, "lon", "lng", "longitude")
        if lat is None:
            lat = _find_key(p, "lat", "latitude")
        if lon is None:
            lon = _find_key(p, "lon", "lng", "longitude")
        if name and lat is not None and lon is not None:
            try:
                loaded[name] = (float(lat), float(lon))
            except (TypeError, ValueError):
                continue
            # Remember every identifier this port goes by, for matching against job records.
            addr_code = _find_key(addr, "code")
            if addr_code:
                PORT_ADDR_CODES[_norm(addr_code)] = name
            for alias in (name, _find_key(p, "code"), _find_key(p, "name"),
                          addr_code, _find_key(addr, "town")):
                a = _norm(alias or "")
                if len(a) >= 4:
                    PORT_ALIASES[a] = name

    if not loaded:
        print("Ferry port data had no usable name/lat/lon - using fallback ports list")
        return

    PORTS = loaded
    NI_PORTS = _resolve_named_ports({"Larne", "Belfast"})
    DIRECT_EU_PORTS = _resolve_named_ports({"Cherbourg"})
    global IRISH_SEA_PORTS
    IRISH_SEA_PORTS = _resolve_named_ports(_IRISH_SEA_KEYWORDS)
    print(f"Loaded {len(PORTS)} ports from Move IT ferryport endpoint")


PORT_ALIASES = {}      # normalized alias -> canonical port name
PORT_ADDR_CODES = {}   # normalized ferryport address code -> canonical port name
TOP_PORTS = []         # most-used ports (keyboard order); empty = show all


_FERRYISH = ("ferry", "boat", "tunnel", "crossing", "sail", "port", "checkin", "check-in")


def rank_ports_by_usage(jobs, top_n=10):
    """Rank ports by how often job history references them. Only trusted evidence counts:
    the job's ferry/crossing field, an exact ferryport address-code match on a stop,
    or a ferry/tunnel-type stop action. Plain delivery addresses in a port town don't count."""
    global TOP_PORTS
    if not jobs:
        print("[port-rank] no job history - keyboard will show all ports")
        return

    aliases = dict(PORT_ALIASES)
    for name in PORTS:  # ensure fallback-mode port names are matchable too
        aliases.setdefault(_norm(name), name)

    def _fuzzy(text):
        n = _norm(text or "")
        if not n:
            return None
        if n in aliases:
            return aliases[n]
        return next((pname for a, pname in aliases.items() if a in n or n in a), None)

    def _fuzzy_all(text):
        """A ferry booking like 'Dublin Port - Holyhead 22:30' names both ends - match every port in it."""
        n = _norm(text or "")
        if not n:
            return set()
        return {pname for a, pname in aliases.items() if a in n}

    counts, matched_jobs = {}, 0
    for job in jobs:
        hits = set()
        ferry_text = _as_text(_find_key(job, "ferrybooking", "ferry", "crossing"))
        if ferry_text:
            hits |= _fuzzy_all(ferry_text)
        for stop in _find_key(job, "stops") or []:
            addr = _find_key(stop, "address")
            addr = addr if isinstance(addr, dict) else {}
            code = _norm(_find_key(addr, "code") or "")
            if code and code in PORT_ADDR_CODES:
                hits.add(PORT_ADDR_CODES[code])
                continue
            action = _stop_action_text(stop)
            if any(k in action for k in _FERRYISH):
                hit = _fuzzy(_find_key(addr, "name")) or _fuzzy(_find_key(addr, "town")) or _fuzzy(_find_key(addr, "code"))
                if hit:
                    hits.add(hit)
        if hits:
            matched_jobs += 1
            for p in hits:
                counts[p] = counts.get(p, 0) + 1

    if not counts:
        print("[port-rank] no port references found in job history - keyboard will show all ports")
        return

    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    TOP_PORTS = [p for p, _ in ranked[:top_n]]
    print(f"[port-rank] {matched_jobs}/{len(jobs)} jobs referenced a port; "
          f"top {len(TOP_PORTS)}: {ranked[:top_n]}")


def port_keyboard_names():
    return TOP_PORTS if TOP_PORTS else list(PORTS.keys())

TRAILER, JOB_CONFIRM, COLLECTION, COUNTRY, PORT, PBN, DOCS, NOTES, CONFIRM = range(9)


def build_keyboard(items, cols=2, extra_row=None):
    rows = [items[i:i + cols] for i in range(0, len(items), cols)]
    if extra_row:
        rows.append(extra_row)
    return ReplyKeyboardMarkup(rows, one_time_keyboard=True, resize_keyboard=True)


def match_port(typed):
    t = typed.lower().strip()
    return next((p for p in PORTS if p.lower() == t or t in p.lower()), None)


def match_country(typed):
    t = typed.lower().strip()
    return next((c for c in COUNTRY_OPTIONS if c.lower() == t), None)


def geocode(place, country=""):
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": f"{place}, {country}", "format": "json", "limit": 1},
            headers={"User-Agent": "OTooleTransportBot/4.0"},
            timeout=8,
        )
        data = r.json()
        if data:
            return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception as e:
        print(f"Geocode error: {e}")
    return None, None


_GEOCODE_CACHE = {}


def _cached_geocode(place, country=""):
    key = f"{place}|{country}".lower()
    if key not in _GEOCODE_CACHE:
        _GEOCODE_CACHE[key] = geocode(place, country)
        time.sleep(1)  # respect Nominatim's 1 req/sec usage policy
    return _GEOCODE_CACHE[key]


def build_frequent_collection_points(days=90, top_n=16, min_count=1, jobs=None):
    """Walk Move IT job history and rank collection addresses by how often they're actually used,
    replacing the static QUICK_POINTS buttons with real, current data."""
    global QUICK_POINTS
    if not API.enabled:
        print("[freq-points] Move IT API disabled - using fallback quick points")
        return

    if jobs is None:
        jobs = API.job_history(days=days)
    print(f"[freq-points] job_history returned {len(jobs)} jobs over last {days} days")
    if not jobs:
        print("[freq-points] no jobs - trying shorter 30-day range in case the API limits the window")
        jobs = API.job_history(days=30)
        print(f"[freq-points] 30-day retry returned {len(jobs)} jobs")
    if not jobs:
        print("[freq-points] still no jobs - using fallback quick points")
        return

    action_codes_seen = set()
    stops_total = 0
    tally = {}
    for job in jobs:
        for stop in _find_key(job, "stops") or []:
            stops_total += 1
            action_codes_seen.add(_as_text(_find_key(stop, "stopaction", "action")) or "?")
            if "collect" not in _stop_action_text(stop):
                continue
            addr = _find_key(stop, "address")
            if not isinstance(addr, dict):
                print(f"[freq-points] collect stop had no address dict, keys were: {list(stop.keys())}")
                continue
            code = _find_key(addr, "code")
            name = _as_text(_find_key(addr, "name")) or code
            if not code or not name:
                print(f"[freq-points] address missing code/name, keys were: {list(addr.keys())}")
                continue
            country = _as_text(_find_key(addr, "country", "countrycode")) or ""
            lat = _find_key(addr, "lat", "latitude")
            lon = _find_key(addr, "lon", "lng", "longitude")
            entry = tally.setdefault(code, {"name": name, "country": country,
                                            "lat": lat, "lon": lon, "count": 0})
            entry["count"] += 1

    print(f"[freq-points] scanned {stops_total} stops; action codes seen: {sorted(action_codes_seen)}")
    print(f"[freq-points] tallied {len(tally)} distinct collection addresses")

    ranked = sorted(tally.values(), key=lambda e: e["count"], reverse=True)
    ranked = [e for e in ranked if e["count"] >= min_count][:top_n]
    if not ranked:
        print("[freq-points] no collection stops matched - check action codes above; using fallback quick points")
        return

    built = {}
    for e in ranked:
        lat, lon = e.get("lat"), e.get("lon")
        if lat is None or lon is None:
            lat, lon = _cached_geocode(e["name"], e["country"])
        if lat is None:
            print(f"[freq-points] no coordinates for '{e['name']}' ({e['country'] or 'no country'}) - skipping")
            continue
        try:
            built[e["name"]] = (e["name"], e["country"], float(lat), float(lon))
        except (TypeError, ValueError):
            continue

    if built:
        QUICK_POINTS = built
        print(f"[freq-points] SUCCESS: loaded {len(QUICK_POINTS)} frequent collection points: {list(QUICK_POINTS)}")
    else:
        print("[freq-points] geocoding failed for all candidates - using fallback quick points")


def route_info(from_lat, from_lon, to_lat, to_lon):
    try:
        r = requests.get(
            f"https://router.project-osrm.org/route/v1/driving/{from_lon},{from_lat};{to_lon},{to_lat}",
            params={"overview": "false"},
            timeout=10,
        )
        data = r.json()
        if data.get("routes"):
            km = data["routes"][0]["distance"] / 1000
            hrs = data["routes"][0]["duration"] / 3600
            return round(km), hrs
    except Exception as e:
        print(f"Routing error: {e}")
    return None, None


def fmt_hours(hrs):
    h = int(hrs)
    m = int(round((hrs - h) * 60))
    if m == 60:
        h, m = h + 1, 0
    return f"{h}h {m:02d}m"


def customs_needed(country, port):
    c = (country or "").lower().strip()
    if c == "northern ireland" and port in NI_PORTS:
        return False
    if port in DIRECT_EU_PORTS and c in EU_COUNTRIES and c != "ireland":
        return False
    return True


def cmr_required(country):
    return (country or "").lower().strip() in (EU_COUNTRIES - {"ireland"})


def compute_route(context):
    d = context.user_data
    port = d.get("port")
    # Prefer live trailer position over collection point
    lat = d.get("live_lat") if d.get("live_lat") is not None else d.get("col_lat")
    lon = d.get("live_lon") if d.get("live_lon") is not None else d.get("col_lon")
    d["eta_source"] = "live trailer position" if d.get("live_lat") is not None else "collection point"
    if lat is None or port not in PORTS:
        d["dist_str"] = "Not calculated"
        d["prep_str"] = "-"
        return
    plat, plon = PORTS[port]
    km, hrs = route_info(lat, lon, plat, plon)
    if km is None:
        d["dist_str"] = "Not calculated"
        d["prep_str"] = "-"
        return
    d["dist_str"] = f"{km} km | {fmt_hours(hrs)} ({d['eta_source']})"
    d["prep_str"] = fmt_hours(max(0.0, hrs - 0.5))


_GB_COUNTRIES = {"uk", "unitedkingdom", "greatbritain", "england", "scotland", "wales"}


def pbn_required(country, port, needs_customs):
    """PBN applies to accompanied RoRo freight moving GB <-> Ireland.
    NI moves and direct EU sailings don't need one."""
    if not needs_customs:
        return False
    c = _norm(country or "")
    return port in IRISH_SEA_PORTS and (c in _GB_COUNTRIES or c == "ireland")


def _deep_find_channel(obj):
    """Search arbitrarily nested JSON for a channel/status value naming a colour."""
    if isinstance(obj, dict):
        val = _norm(_as_text(_find_key(obj, "channel", "status", "pbnchannel", "routing", "channeldescription")) or "")
        for s in ("green", "orange", "red"):
            if s in val:
                return s
        for v in obj.values():
            found = _deep_find_channel(v)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _deep_find_channel(v)
            if found:
                return found
    return None


def _map_ros_channel(blob):
    """Map Revenue's channel wording to our status buckets."""
    b = blob.lower()
    if "exit" in b:
        return "green"
    if any(k in b for k in ("customs", "inspection", "dafm", "sps", "bip", "red")):
        return "red"
    if any(k in b for k in ("parking", "orange", "documentary")):
        return "orange"
    if any(k in b for k in ("not yet", "not available", "pending", "not released")):
        return "pending"
    return ""


def _parse_ros_result(html):
    """Parse Revenue's HTML result page. Reads both the visible channel label and the
    FreightLookupResult debug comment (belt and braces - either may change)."""
    low = html.lower()

    fields = {}
    m = re.search(r"freightlookupresult\((.*?)\)", low, re.S)
    if m:
        for part in m.group(1).split(","):
            if "=" in part:
                k, v = part.split("=", 1)
                fields[k.strip()] = v.strip()

    if fields.get("recordfound") == "false":
        return "notfound", ""

    mlab = re.search(r'laneresultlane[^"]*"\s*>\s*([^<]+)', low)
    label = (mlab.group(1).strip() if mlab else "")
    channel_raw = fields.get("channel", "").replace("_", " ")
    lane_raw = fields.get("lane", "").replace("_", " ")
    detail = (label or channel_raw).title()

    status = _map_ros_channel(" ".join([label, channel_raw, lane_raw]))
    if not status and fields.get("lanereleased") == "false":
        return "pending", detail
    if not status and ("no channel" in low or "not yet available" in low):
        return "pending", detail
    return (status or "unknown"), detail


def _check_pbn_ros(pbn):
    """Full ROS look-up flow: GET page (CSRF + session), then POST the form.
    Tries today's arrival date, tomorrow's (after-midnight sailings), then blank."""
    try:
        s = requests.Session()
        s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) OTooleTransportBot/5.0",
            "Accept": "text/html,application/xhtml+xml",
        })
        r = s.get(ROS_PBN_URL, timeout=15)
        m = re.search(r'name="_csrf"[^>]*value="([^"]+)"', r.text)
        csrf = m.group(1) if m else ""

        last = ("unknown", "no response")
        for ad in (date.today().strftime("%d-%m-%Y"),
                   (date.today() + timedelta(days=1)).strftime("%d-%m-%Y"),
                   ""):
            form = {"pbnID": pbn, "registrationNumber": "", "arrivalDate": ad,
                    "arrivalKey": "", "pbnLookUp": "true", "_csrf": csrf}
            r2 = s.post(ROS_PBN_URL, data=form, timeout=15)
            if r2.status_code != 200:
                last = ("unknown", f"lookup returned HTTP {r2.status_code}")
                continue
            status, detail = _parse_ros_result(r2.text)
            if status == "notfound":
                last = ("unknown", "PBN not found - check the number")
                continue
            return status, detail
        return last
    except Exception as e:
        return "unknown", str(e)


def check_pbn_status(pbn):
    """Check the PBN channel. Returns (status, detail);
    status is green/orange/red/pending/unknown, detail is Revenue's official wording."""
    if not ROS_PBN_URL:
        return "unknown", "automatic check not configured"
    if "ros.ie" in ROS_PBN_URL:
        return _check_pbn_ros(pbn)
    # Generic JSON endpoint mode (e.g. a proxy)
    try:
        url = (ROS_PBN_URL.format(pbn=pbn) if "{pbn}" in ROS_PBN_URL
               else ROS_PBN_URL.rstrip("/") + "/" + pbn)
        headers = {"Accept": "application/json",
                   "User-Agent": "Mozilla/5.0 (OTooleTransportBot/5.0)"}
        if ROS_PBN_METHOD == "POST":
            body = (ROS_PBN_BODY or '{"pbnId": "{pbn}"}').replace("{pbn}", pbn)
            headers["Content-Type"] = "application/json"
            r = requests.post(url, data=body, headers=headers, timeout=10)
        else:
            r = requests.get(url, headers=headers, timeout=10)
        if r.status_code == 404:
            return "unknown", "PBN not found - check the number"
        if r.status_code != 200:
            return "unknown", f"lookup returned HTTP {r.status_code}"
        # Prefer structured JSON if the endpoint returns it
        try:
            found = _deep_find_channel(r.json())
            if found:
                return found, ""
        except ValueError:
            pass
        text = r.text.lower()
        for s in ("green", "orange", "red"):
            if re.search(rf"\b{s}\b", text):
                return s, ""
        if any(k in text for k in ("not available", "not yet", "pending", "no channel")):
            return "pending", ""
        return "unknown", "unrecognised response format"
    except Exception as e:
        return "unknown", str(e)


def post_pbn_alert(data, status):
    """Immediate compact Teams alert for red/orange/missing PBN - doesn't wait for
    the driver to finish the flow, because the team needs lead time."""
    labels = {"red": ("RED PBN - customs check required", "Attention"),
              "orange": ("ORANGE PBN - documentary check required", "Warning"),
              "missing": ("NO PBN - driver has no Pre-Boarding Notification", "Attention")}
    header, color = labels.get(status, (f"PBN status: {status}", "Warning"))
    facts = [
        {"title": "Trailer", "value": data.get("trailer", "-")},
        {"title": "PBN", "value": data.get("pbn") or "NOT PROVIDED"},
        {"title": "Port", "value": data.get("port", "-")},
        {"title": "Collection", "value": f"{data.get('collection', '-')} ({data.get('country', '-')})"},
    ]
    if data.get("live_text"):
        facts.append({"title": "Trailer last seen", "value": data["live_text"]})
    card = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard", "version": "1.4",
        "body": [
            {"type": "Container", "style": "emphasis", "bleed": True,
             "items": [{"type": "TextBlock", "text": header, "weight": "Bolder",
                        "size": "Medium", "color": color, "wrap": True}]},
            {"type": "FactSet", "facts": facts, "separator": True},
        ],
    }
    body = {"type": "message",
            "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": card}]}
    try:
        r = requests.post(TEAMS_WEBHOOK, json=body, timeout=10)
        return r.status_code in (200, 202)
    except Exception as e:
        print(f"Teams PBN alert error: {e}")
        return False


def post_to_teams(data):
    needs_customs = data.get("needs_customs", True)
    missing_cmr = data.get("cmr_missing", False)
    pbn_status = data.get("pbn_status")
    if not needs_customs:
        header, color = "Driver submission - no customs needed", "Good"
    elif pbn_status in ("red", "orange", "missing"):
        header, color = f"Driver submission - customs required, PBN {pbn_status.upper()}", "Attention"
    elif missing_cmr:
        header, color = "Driver submission - customs required, CMR MISSING", "Warning"
    else:
        header, color = "Driver submission - customs required", "Attention"

    facts = [
        {"title": "Trailer", "value": data.get("trailer", "-")},
        {"title": "Collection", "value": f"{data.get('collection', '-')} ({data.get('country', '-')})"},
        {"title": "Port", "value": data.get("port", "-")},
        {"title": "Distance / ETA", "value": data.get("dist_str", "-")},
        {"title": "Time to prep docs", "value": data.get("prep_str", "-")},
        {"title": "Documents", "value": data.get("docs", "None")},
        {"title": "Notes", "value": data.get("notes", "None")},
        {"title": "Submitted", "value": data.get("submitted", "-")},
    ]
    if data.get("pbn") or pbn_status:
        facts.insert(3, {"title": "PBN",
                         "value": f"{data.get('pbn') or 'NOT PROVIDED'} - {(pbn_status or 'unchecked').upper()}"})
    job = data.get("job_summary")
    if job:
        if job.get("job_id"):
            facts.insert(1, {"title": "Move IT job", "value": str(job["job_id"])})
        if job.get("customer"):
            facts.insert(2, {"title": "Customer", "value": job["customer"]})
        if job.get("delivery"):
            facts.append({"title": "Delivery", "value": job["delivery"]})
        if job.get("ferry"):
            facts.append({"title": "Ferry booking", "value": job["ferry"]})
    if data.get("live_text"):
        facts.append({"title": "Trailer last seen", "value": data["live_text"]})

    card = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": [
            {
                "type": "Container",
                "style": "emphasis",
                "bleed": True,
                "items": [
                    {"type": "TextBlock", "text": header, "weight": "Bolder",
                     "size": "Medium", "color": color, "wrap": True}
                ],
            },
            {"type": "FactSet", "facts": facts, "separator": True},
        ],
        "actions": (
            [{"type": "Action.OpenUrl", "title": "View uploaded document", "url": data["doc_url"]}]
            if data.get("doc_url") else []
        ),
    }
    body = {
        "type": "message",
        "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": card}],
    }
    try:
        r = requests.post(TEAMS_WEBHOOK, json=body, timeout=10)
        return r.status_code in (200, 202)
    except Exception as e:
        print(f"Teams post error: {e}")
        return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "Hi! I'm the O'Toole Transport customs bot.\n\nWhat is your *trailer number*?",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
    return TRAILER


async def get_trailer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    trailer_code = update.message.text.strip().upper()
    context.user_data["trailer"] = trailer_code

    # --- Move IT lookup: trailer + live position + today's job ---
    if API.enabled:
        trailer = API.search_trailer(trailer_code)
        if trailer:
            loc = _find_key(trailer, "trackinglocation")
            if isinstance(loc, dict) and _find_key(loc, "lat") is not None:
                context.user_data["live_lat"] = _find_key(loc, "lat")
                context.user_data["live_lon"] = _find_key(loc, "lon")
                text = _find_key(loc, "locationtext")
                when = _find_key(loc, "recordedon")
                context.user_data["live_text"] = f"{text or ''} {('(' + str(when)[:16] + ')') if when else ''}".strip()

            trailer_id = _find_key(trailer, "id")
            for job in API.todays_jobs():
                if job_matches_trailer(job, trailer_code, trailer_id):
                    s = extract_job_summary(job)
                    context.user_data["job_summary"] = s
                    if s.get("collection"):
                        context.user_data["collection"] = s["collection"]
                    lines = [f"*Found your job in Move IT:*\n"]
                    if s.get("customer"):
                        lines.append(f"Customer: {md(s['customer'])}")
                    if s.get("collection"):
                        lines.append(f"Collection: {md(s['collection'])}")
                    if s.get("delivery"):
                        lines.append(f"Delivery: {md(s['delivery'])}")
                    lines.append("\nIs this your load?")
                    await update.message.reply_text(
                        "\n".join(lines),
                        parse_mode="Markdown",
                        reply_markup=build_keyboard(["Yes, that's my load", "No - enter details myself"], cols=1),
                    )
                    return JOB_CONFIRM

    # --- Fallback: manual v3 flow ---
    names = list(QUICK_POINTS.keys())
    hint = "\n\nOr *type a few letters* to search all locations." if ADDRESS_BOOK else ""
    await update.message.reply_text(
        f"Trailer *{md(trailer_code)}* noted.\n\nWhere did you *collect the load*? Tap one:{hint}",
        parse_mode="Markdown",
        reply_markup=build_keyboard(names, cols=2, extra_row=["Other location"]),
    )
    return COLLECTION


async def job_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "yes" in update.message.text.lower():
        # Collection known from job. Derive country from job if possible, else ask.
        s = context.user_data.get("job_summary", {})
        from_c = (s.get("from_c") or "").strip()
        mapped = match_country(from_c) if from_c else None
        if from_c.upper() == "EU":
            mapped = None  # corridor-level, need real country for customs rules
        if mapped:
            context.user_data["country"] = mapped
            return await ask_port(update, context)
        return await ask_country(update, context)
    # Driver says the job is wrong - manual flow
    names = list(QUICK_POINTS.keys())
    await update.message.reply_text(
        "No problem. Where did you *collect the load*? Tap one:",
        parse_mode="Markdown",
        reply_markup=build_keyboard(names, cols=2, extra_row=["Other location"]),
    )
    return COLLECTION


async def get_collection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choice = update.message.text.strip()

    if choice in QUICK_POINTS:
        town, country, lat, lon = QUICK_POINTS[choice]
        context.user_data.update(
            {"collection": choice, "country": country, "col_lat": lat, "col_lon": lon}
        )
        if not country:
            return await ask_country(update, context)
        return await ask_port(update, context)

    if choice == "Other location":
        await update.message.reply_text(
            "Type the *place name* (or first few letters to search):",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardRemove(),
        )
        return COLLECTION

    matches = context.user_data.get("search_matches", {})
    if choice in matches:
        loc = matches[choice]
        lat, lon = geocode(f"{loc['name']}, {loc['city']}", loc["country"])
        if lat is None and loc["city"]:
            lat, lon = geocode(loc["city"], loc["country"])
        context.user_data.update(
            {"collection": loc["name"], "country": loc["country"] or "Other",
             "col_lat": lat, "col_lon": lon}
        )
        if not loc["country"]:
            return await ask_country(update, context)
        return await ask_port(update, context)

    if choice == "None of these - use what I typed":
        context.user_data["collection"] = context.user_data.get("last_query", choice).title()
        return await ask_country(update, context)

    results = search_locations(choice)
    if results:
        context.user_data["last_query"] = choice
        labels, m = [], {}
        for loc in results:
            label = f"{loc['name']} - {loc['city']}"[:60] if loc["city"] else loc["name"][:60]
            labels.append(label)
            m[label] = loc
        context.user_data["search_matches"] = m
        await update.message.reply_text(
            f"Found these for *{md(choice)}* - tap one:",
            parse_mode="Markdown",
            reply_markup=build_keyboard(labels, cols=1, extra_row=["None of these - use what I typed"]),
        )
        return COLLECTION

    context.user_data["collection"] = choice.title()
    return await ask_country(update, context)


async def ask_country(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Which *country* did you load in?",
        parse_mode="Markdown",
        reply_markup=build_keyboard(COUNTRY_OPTIONS, cols=3),
    )
    return COUNTRY


async def get_country(update: Update, context: ContextTypes.DEFAULT_TYPE):
    typed = update.message.text.strip()
    context.user_data["country"] = match_country(typed) or typed
    if context.user_data.get("col_lat") is None and context.user_data.get("live_lat") is None:
        lat, lon = geocode(context.user_data.get("collection", ""), context.user_data["country"])
        context.user_data["col_lat"] = lat
        context.user_data["col_lon"] = lon
    return await ask_port(update, context)


async def ask_port(update: Update, context: ContextTypes.DEFAULT_TYPE):
    names = port_keyboard_names()
    hint = "\n\nOr *type* any other port." if TOP_PORTS else ""
    await update.message.reply_text(
        f"Which *port* are you heading to?{hint}",
        parse_mode="Markdown",
        reply_markup=build_keyboard(names, cols=2),
    )
    return PORT


async def get_port(update: Update, context: ContextTypes.DEFAULT_TYPE):
    typed = update.message.text.strip()
    context.user_data["port"] = match_port(typed) or typed
    context.user_data["chat_id"] = update.effective_chat.id

    country = context.user_data.get("country", "")
    needs = customs_needed(country, context.user_data["port"])
    context.user_data["needs_customs"] = needs

    if pbn_required(country, context.user_data["port"], needs):
        return await ask_pbn(update, context)
    return await ask_docs(update, context)


async def ask_docs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    country = context.user_data.get("country", "")
    if not context.user_data.get("needs_customs", True):
        await update.message.reply_text(
            "*No customs needed for this route!* You're good to go.\n\n"
            "I'll log it with the team. Any documents to attach anyway? Send them or tap Skip.",
            parse_mode="Markdown",
            reply_markup=build_keyboard(["Skip"], cols=1),
        )
    elif cmr_required(country):
        await update.message.reply_text(
            "*Please send a photo of the CMR now.*\n\n"
            "The customs team needs it to prepare your paperwork. "
            "Also send any supplier documents you were given.",
            parse_mode="Markdown",
            reply_markup=build_keyboard(["I don't have the CMR"], cols=1),
        )
    else:
        await update.message.reply_text(
            "Do you have any *MRN documents or photos* from the collection point?\n\nSend them now or tap Skip.",
            parse_mode="Markdown",
            reply_markup=build_keyboard(["Skip"], cols=1),
        )
    return DOCS


async def ask_pbn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "This is a *GB - Ireland crossing*, so I need your *PBN* "
        "(Pre-Boarding Notification).\n\n"
        "Type the PBN number from your booking, or tap below.",
        parse_mode="Markdown",
        reply_markup=build_keyboard(["I don't have a PBN"], cols=1),
    )
    return PBN


async def get_pbn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    d = context.user_data

    # Second pass: driver reporting what they saw on Revenue's look-up page
    if d.get("pbn_await_manual"):
        low = text.lower()
        if "green" in low:
            d["pbn_status"], d["pbn_await_manual"] = "green", False
            await update.message.reply_text(
                "🟢 *Green channel* - you're good to sail. Carry on!",
                parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
            return await ask_docs(update, context)
        if "orange" in low or "red" in low:
            d["pbn_status"], d["pbn_await_manual"] = ("red" if "red" in low else "orange"), False
            post_pbn_alert(d, d["pbn_status"])
            await update.message.reply_text(
                f"🔴 *{d['pbn_status'].upper()} channel* - a customs check is required at the port.\n\n"
                "I've alerted the customs team. Follow the port signs for checks; "
                "the team will message you here.",
                parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
            return await ask_docs(update, context)
        if "can't" in low or "cant" in low or "later" in low:
            d["pbn_status"], d["pbn_await_manual"] = "unchecked", False
            await update.message.reply_text(
                "No problem - the channel usually opens about *30 minutes before arrival*. "
                "Check the link again closer to the port.",
                parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
            return await ask_docs(update, context)
        await update.message.reply_text(
            "Please tap one of the options.",
            reply_markup=build_keyboard(["It's Green", "It's Orange", "It's Red", "Can't check now"], cols=2))
        return PBN

    if text.lower() == "i don't have a pbn":
        d["pbn"], d["pbn_status"] = None, "missing"
        post_pbn_alert(d, "missing")
        await update.message.reply_text(
            "OK - *you can't board without a PBN*, so I've alerted the customs team now. "
            "They'll create one and send it to you here. Keep driving, but *don't check in at the port* "
            "until you have it.",
            parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
        return await ask_docs(update, context)

    pbn = re.sub(r"[\s\-]", "", text).upper()
    if pbn.startswith("PBN") and len(pbn) > 9:
        pbn = pbn[3:]  # driver typed the word PBN before the ID
    if not re.fullmatch(r"[A-Z0-9]{6,15}", pbn) or not any(ch.isdigit() for ch in pbn):
        await update.message.reply_text(
            "That doesn't look like a PBN ID. It's usually *8 letters and numbers* "
            "(e.g. YL22UY97) - you'll find it on your booking.\n\nTry again, or tap below.",
            parse_mode="Markdown",
            reply_markup=build_keyboard(["I don't have a PBN"], cols=1),
        )
        return PBN
    d["pbn"] = pbn

    status, detail = check_pbn_status(pbn)
    if status == "green":
        d["pbn_status"] = "green"
        await update.message.reply_text(
            f"🟢 *{md(detail) if detail else 'Green channel'}* - you're good to sail. Carry on!",
            parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
        return await ask_docs(update, context)
    if status in ("red", "orange"):
        d["pbn_status"] = status
        post_pbn_alert(d, status)
        await update.message.reply_text(
            f"🔴 *{md(detail) if detail else status.upper() + ' channel'}* - "
            "a check is required at the port.\n\n"
            "I've alerted the customs team. Follow the port signs for checks; "
            "the team will message you here.",
            parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
        return await ask_docs(update, context)
    if status == "pending" and getattr(context, "job_queue", None):
        d["pbn_status"] = "pending"
        context.job_queue.run_repeating(
            pbn_recheck_job, interval=600, first=600,
            data={"pbn": pbn, "chat_id": update.effective_chat.id,
                  "attempts": 0, "snapshot": dict(d)},
            name=f"pbn-{update.effective_chat.id}",
        )
        await update.message.reply_text(
            f"PBN *{md(pbn)}* noted. The channel isn't decided yet - it usually opens about "
            "*30 minutes before arrival*. I'll keep checking every 10 minutes and message you "
            "the moment it changes.",
            parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
        return await ask_docs(update, context)

    # Automatic check unavailable - guide the driver through the public look-up page
    d["pbn_status"] = "unchecked"
    d["pbn_await_manual"] = True
    await update.message.reply_text(
        f"PBN *{md(pbn)}* noted. I can't check the channel automatically right now"
        f"{' (' + md(detail) + ')' if detail else ''}.\n\n"
        f"Open Revenue's look-up and check it:\n{PBN_LOOKUP_PAGE}\n\nWhat does it say?",
        parse_mode="Markdown",
        reply_markup=build_keyboard(["It's Green", "It's Orange", "It's Red", "Can't check now"], cols=2),
    )
    return PBN


async def pbn_recheck_job(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    status, _ = check_pbn_status(data["pbn"])
    if status == "green":
        await context.bot.send_message(
            data["chat_id"],
            "🟢 *Your PBN just went GREEN* - you're good to sail. Safe drive!",
            parse_mode="Markdown")
        context.job.schedule_removal()
        return
    if status in ("red", "orange"):
        snap = dict(data["snapshot"])
        snap["pbn_status"] = status
        post_pbn_alert(snap, status)
        await context.bot.send_message(
            data["chat_id"],
            f"🔴 *Your PBN channel is {status.upper()}* - a customs check is required at the port. "
            "The customs team has been alerted and will message you here.",
            parse_mode="Markdown")
        context.job.schedule_removal()
        return
    data["attempts"] += 1
    if data["attempts"] >= 12:  # ~2 hours
        await context.bot.send_message(
            data["chat_id"],
            f"I couldn't get a channel decision for your PBN after 2 hours. "
            f"Please check it yourself before the port:\n{PBN_LOOKUP_PAGE}")
        context.job.schedule_removal()


async def get_docs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip().lower()
    if text == "skip":
        context.user_data["docs"] = "None"
        context.user_data["doc_url"] = None
    elif text == "i don't have the cmr":
        context.user_data["docs"] = "CMR NOT PROVIDED"
        context.user_data["doc_url"] = None
        context.user_data["cmr_missing"] = True
    elif update.message.photo:
        try:
            file = await update.message.photo[-1].get_file()
            context.user_data["doc_url"] = file.file_path
            context.user_data["docs"] = "Photo uploaded"
        except Exception as e:
            print(f"File fetch error: {e}")
            context.user_data["doc_url"] = None
            context.user_data["docs"] = "Photo uploaded (link unavailable)"
    elif update.message.document:
        try:
            file = await update.message.document.get_file()
            context.user_data["doc_url"] = file.file_path
            context.user_data["docs"] = update.message.document.file_name
        except Exception as e:
            print(f"File fetch error: {e}")
            context.user_data["doc_url"] = None
            context.user_data["docs"] = f"{update.message.document.file_name} (link unavailable)"
    else:
        context.user_data["docs"] = update.message.text or "None"
        context.user_data["doc_url"] = None

    await update.message.reply_text(
        "Any *additional notes* for the customs team?\n\nType them or tap Skip.",
        parse_mode="Markdown",
        reply_markup=build_keyboard(["Skip"], cols=1),
    )
    return NOTES


async def get_notes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    context.user_data["notes"] = "None" if text.lower() == "skip" else text

    compute_route(context)

    d = context.user_data
    if not d.get("needs_customs", True):
        status_line = "\nNo customs needed - good to go\n"
    elif d.get("cmr_missing"):
        status_line = "\nCMR missing - the team will follow up\n"
    else:
        status_line = ""
    job = d.get("job_summary") or {}
    job_line = f"Job: {md(job['job_id'])}\n" if job.get("job_id") else ""
    pbn_line = ""
    if d.get("pbn") or d.get("pbn_status"):
        pbn_line = f"PBN: {md(d.get('pbn') or 'NOT PROVIDED')} ({md((d.get('pbn_status') or 'unchecked').upper())})\n"
    summary = (
        f"*Please check your summary:*\n{status_line}\n"
        f"{job_line}"
        f"Trailer: *{md(d.get('trailer'))}*\n"
        f"Collection: {md(d.get('collection'))} ({md(d.get('country'))})\n"
        f"Port: {md(d.get('port'))}\n"
        f"{pbn_line}"
        f"Distance: {md(d.get('dist_str'))}\n"
        f"Docs: {md(d.get('docs'))}\n"
        f"Notes: {md(d.get('notes'))}"
    )
    kb = build_keyboard(["Send to customs team", "Start again"], cols=1)
    try:
        await update.message.reply_text(summary, parse_mode="Markdown", reply_markup=kb)
    except Exception as e:
        print(f"Summary Markdown send failed ({e}) - retrying as plain text")
        await update.message.reply_text(summary.replace("\\", "").replace("*", ""), reply_markup=kb)
    return CONFIRM


async def confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "start again" in update.message.text.lower():
        return await start(update, context)

    context.user_data["submitted"] = datetime.now().strftime("%d/%m %H:%M")
    success = post_to_teams(context.user_data)
    if success:
        if not context.user_data.get("needs_customs", True):
            msg = "*Logged with the team.*\n\nNo customs needed - you're good to go!\n\nSafe drive!"
        else:
            msg = "*Customs team notified!*\n\nThey'll prepare your paperwork and message you here if anything is needed.\n\nSafe drive!"
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
    else:
        await update.message.reply_text(
            "Something went wrong sending to Teams. Please call the customs team directly.",
            reply_markup=ReplyKeyboardRemove(),
        )
    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled. Type /start to begin again.", reply_markup=ReplyKeyboardRemove())
    context.user_data.clear()
    return ConversationHandler.END


def refresh_datasets():
    """Slow: pulls up to 90 days of job history (with per-day retries around Move IT's
    null-date 500s). Runs in a background thread so the bot answers drivers immediately;
    fallback quick points / full port list serve until this finishes."""
    try:
        history = API.job_history() if API.enabled else []
        build_frequent_collection_points(jobs=history)
        rank_ports_by_usage(history)
        print("[datasets] background refresh complete")
    except Exception as e:
        print(f"[datasets] background refresh failed: {e} - fallback lists remain active")


def main():
    load_address_book()
    load_ports_from_api()  # fast: single call, worth doing before polling starts
    threading.Thread(target=refresh_datasets, daemon=True).start()
    print(f"Move IT API: {'ENABLED at ' + MOVEIT_BASE_URL if API.enabled else 'not configured - manual flow only'}")
    app = Application.builder().token(BOT_TOKEN).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start), MessageHandler(filters.TEXT & ~filters.COMMAND, start)],
        states={
            TRAILER:     [MessageHandler(filters.TEXT & ~filters.COMMAND, get_trailer)],
            JOB_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, job_confirm)],
            COLLECTION:  [MessageHandler(filters.TEXT & ~filters.COMMAND, get_collection)],
            COUNTRY:     [MessageHandler(filters.TEXT & ~filters.COMMAND, get_country)],
            PORT:        [MessageHandler(filters.TEXT & ~filters.COMMAND, get_port)],
            PBN:         [MessageHandler(filters.TEXT & ~filters.COMMAND, get_pbn)],
            DOCS:        [MessageHandler(filters.TEXT | filters.PHOTO | filters.Document.ALL, get_docs)],
            NOTES:       [MessageHandler(filters.TEXT & ~filters.COMMAND, get_notes)],
            CONFIRM:     [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(conv)
    print("Bot v5 is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
