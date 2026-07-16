import os
import csv
import requests
from datetime import datetime, date
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

    def _get(self, path, params=None):
        if not self.enabled:
            return None
        try:
            r = self.session.get(f"{MOVEIT_BASE_URL}/{path.lstrip('/')}", params=params, timeout=10)
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


# =====================================================================
# JOB PARSING - trailer + collection/delivery live inside the stops array
# ---------------------------------------------------------------------
# A Move IT job looks like:
#   {"id":1532335,
#    "customer":{"id":328,"code":"GLO001","name":"Global Fruit Company Ltd."},
#    "stops":[{"id":..,"jobid":..,"groupid":..,
#              "stopdatetime":"2026-07-15T11:00:00",
#              "stopaction":{"id":13,"code":"Drop Trailer"},
#              "address":{"id":115,"code":"Global Fruit-finglas","name":"Global Fruit",...}}]}
# The trailer is NOT a documented top-level field, so job_matches_trailer()
# walks the ENTIRE structure (top level + every stop) looking for the code
# anywhere, or the trailer id inside a trailer-ish subtree.
# Which stop is collection vs delivery is decided from the stopaction code,
# with a positional fallback (first stop = collection, last = delivery) for
# codes we don't yet recognise. Adjust the keyword lists below once the real
# stopaction code set is confirmed from the live logs.
# =====================================================================

# stopaction.code keywords -> role. Matched as substrings of the normalised code.
STOP_COLLECT_KEYWORDS = ("collect", "load", "pick")
STOP_DELIVER_KEYWORDS = ("deliver", "unload", "drop", "tip", "offload")

# keys whose subtree may hold a trailer id we can match on
_TRAILERISH_KEYS = ("trailer", "trailercode", "trailerreg", "registration",
                    "reg", "vehicle", "equipment", "resource", "asset", "unit")


def _stop_action_code(stop):
    action = _find_key(stop, "stopaction", "action", "activity")
    if isinstance(action, dict):
        return str(_find_key(action, "code", "name", "description") or "")
    return str(action or "")


def _stop_address_dict(stop):
    addr = _find_key(stop, "address", "location", "site")
    return addr if isinstance(addr, dict) else None


def _stop_address_text(stop):
    addr = _stop_address_dict(stop)
    if addr:
        name = _find_key(addr, "name")
        city = _find_key(addr, "city", "town")
        parts = [str(p) for p in (name, city) if p]
        if parts:
            # avoid "Global Fruit, Global Fruit" style duplicates
            if len(parts) == 2 and _norm(parts[1]) in _norm(parts[0]):
                return parts[0]
            return ", ".join(parts)
        code = _find_key(addr, "code")
        return str(code) if code else None
    return _as_text(_find_key(stop, "address", "location", "site"))


def _stop_country(stop):
    addr = _stop_address_dict(stop)
    if addr:
        return _as_text(_find_key(addr, "country", "countryname", "country_name", "countrycode"))
    return None


def _stop_when(stop):
    return str(_find_key(stop, "stopdatetime", "datetime", "date", "eta", "plannedarrival") or "")


def _classify_stop(stop):
    c = _norm(_stop_action_code(stop))
    if any(k in c for k in STOP_COLLECT_KEYWORDS):
        return "collect"
    if any(k in c for k in STOP_DELIVER_KEYWORDS):
        return "deliver"
    return "other"


def _job_stops(job):
    stops = _find_key(job, "stops", "stop", "legs", "movements")
    if not isinstance(stops, list):
        return []
    stops = [s for s in stops if isinstance(s, dict)]
    # ISO datetimes sort correctly as plain strings; stable sort keeps API order on ties
    return sorted(stops, key=_stop_when)


def extract_job_summary(job):
    """Pull the useful fields out of a job record - trailer/collection/delivery
    live inside the stops array, not at the top level."""
    stops = _job_stops(job)

    collect_stop = None
    deliver_stop = None
    for s in stops:
        role = _classify_stop(s)
        if role == "collect" and collect_stop is None:
            collect_stop = s
        elif role == "deliver":
            deliver_stop = s  # last delivery wins (final drop)

    # Positional fallback for codes we don't recognise yet.
    if collect_stop is None and stops:
        collect_stop = stops[0]
    if deliver_stop is None and len(stops) > 1:
        deliver_stop = stops[-1]

    # Never report the same physical stop as both collection and delivery
    # (e.g. a single "Drop Trailer" stop -> collection only, no delivery).
    if deliver_stop is collect_stop:
        deliver_stop = None

    return {
        "job_id": _find_key(job, "id", "jobid", "job #", "job_no", "jobnumber"),
        "customer": _as_text(_find_key(job, "customer", "customer name", "client")),
        "collection": _stop_address_text(collect_stop) if collect_stop else None,
        "delivery": _stop_address_text(deliver_stop) if deliver_stop else None,
        "from_c": _stop_country(collect_stop) if collect_stop else None,
        "to_c": _stop_country(deliver_stop) if deliver_stop else None,
        "ferry": _as_text(_find_key(job, "ferrybooking", "ferry", "crossing")),
    }


def job_matches_trailer(job, trailer_code, trailer_id):
    """Does this job reference our trailer? The trailer isn't a fixed top-level
    field, so walk the whole job (incl. every stop): match the code anywhere,
    or the id when it sits inside a trailer-ish subtree (avoids colliding with
    the many other ids in a job - stop ids, address ids, customer id, etc.)."""
    code_norm = _norm(trailer_code) if trailer_code else ""
    hit = {"found": False}

    def walk(node, in_trailer_ctx):
        if hit["found"]:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                child_ctx = in_trailer_ctx or any(t in _norm(k) for t in _TRAILERISH_KEYS)
                walk(v, child_ctx)
        elif isinstance(node, list):
            for v in node:
                walk(v, in_trailer_ctx)
        elif node is not None:
            if code_norm and _norm(node) == code_norm:
                hit["found"] = True
                return
            if trailer_id is not None and in_trailer_ctx:
                try:
                    if int(node) == int(trailer_id):
                        hit["found"] = True
                except (ValueError, TypeError):
                    pass

    walk(job, False)
    return hit["found"]


def suggested_port_from_job(job, summary):
    """Best-effort: pull a likely departure port from ferry/route fields on the
    job so we can offer a one-tap confirm. Returns a PORTS key or None.
    Only looks at explicit ferry/route/port fields (not addresses) to avoid
    false matches. Improve once the ferry JSON shape is confirmed."""
    def port_in(text):
        if not text:
            return None
        tl = str(text).lower()
        for p in PORTS:
            if p.lower() in tl:
                return p
        return None

    for key in ("departureport", "departure_port", "ferryport", "port",
                "route", "crossing", "ferry", "ferrybooking", "sailing"):
        p = port_in(_as_text(_find_key(job, key)))
        if p:
            return p
    return port_in(summary.get("ferry"))


QUICK_POINTS = {
    "ABP Cahir":            ("Cahir", "Ireland", 52.3775, -7.9268),
    "G's Fresh Barway":     ("Barway, Ely", "UK", 52.3480, 0.2735),
    "Florette Lichfield":   ("Lichfield", "UK", 52.6816, -1.8317),
    "Delanchy Boulogne":    ("Boulogne-sur-Mer", "France", 50.7264, 1.6147),
    "La Boulangere":        ("Mortagne-sur-Sevre", "France", 46.9922, -0.9469),
    "Alpro Wevelgem":       ("Wevelgem", "Belgium", 50.8050, 3.1740),
    "Clarebout Mouscron":   ("Mouscron", "Belgium", 50.7439, 3.2062),
    "RDV Marck":            ("Marck", "France", 50.9490, 1.9520),
}

ADDRESS_BOOK = []

def load_address_book():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locations.csv")
    if not os.path.exists(path):
        print("No locations.csv found - running with quick buttons only")
        return
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                name = (row.get("Name") or "").strip()
                if name:
                    ADDRESS_BOOK.append({
                        "name": name,
                        "city": (row.get("City") or "").strip(),
                        "country": (row.get("Country") or "").strip(),
                    })
        print(f"Loaded {len(ADDRESS_BOOK)} locations from locations.csv")
    except Exception as e:
        print(f"Could not load locations.csv: {e}")


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


PORTS = {
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

EU_COUNTRIES = {"ireland", "france", "germany", "netherlands", "belgium", "spain", "italy", "poland"}
NI_PORTS = {"Larne", "Belfast"}
DIRECT_EU_PORTS = {"Cherbourg"}
COUNTRY_OPTIONS = ["UK", "Northern Ireland", "Ireland", "France", "Germany",
                   "Netherlands", "Belgium", "Spain", "Italy", "Poland", "Other"]

# Map free-text country values from Move IT (GB, IE, "United Kingdom", ...) to our options.
COUNTRY_ALIASES = {
    "gb": "UK", "uk": "UK", "united kingdom": "UK", "great britain": "UK",
    "england": "UK", "scotland": "UK", "wales": "UK", "gbr": "UK",
    "ni": "Northern Ireland", "northern ireland": "Northern Ireland", "xi": "Northern Ireland",
    "ie": "Ireland", "irl": "Ireland", "ireland": "Ireland",
    "republic of ireland": "Ireland", "eire": "Ireland",
    "fr": "France", "fra": "France", "france": "France",
    "de": "Germany", "deu": "Germany", "germany": "Germany", "deutschland": "Germany",
    "nl": "Netherlands", "nld": "Netherlands", "netherlands": "Netherlands", "holland": "Netherlands",
    "be": "Belgium", "bel": "Belgium", "belgium": "Belgium",
    "es": "Spain", "esp": "Spain", "spain": "Spain",
    "it": "Italy", "ita": "Italy", "italy": "Italy",
    "pl": "Poland", "pol": "Poland", "poland": "Poland",
}

TRAILER, JOB_CONFIRM, COLLECTION, COUNTRY, PORT, PORT_CONFIRM, DOCS, NOTES, CONFIRM = range(9)


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


def match_country_loose(typed):
    """Accepts free-text/ISO country values from Move IT and maps to a COUNTRY_OPTION."""
    if not typed:
        return None
    t = str(typed).lower().strip()
    if t in COUNTRY_ALIASES:
        return COUNTRY_ALIASES[t]
    return match_country(typed)


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


def post_to_teams(data):
    needs_customs = data.get("needs_customs", True)
    missing_cmr = data.get("cmr_missing", False)
    if not needs_customs:
        header, color = "Driver submission - no customs needed", "Good"
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
                    context.user_data["suggested_port"] = suggested_port_from_job(job, s)
                    if s.get("collection"):
                        context.user_data["collection"] = s["collection"]
                    lines = ["*Found your job in Move IT:*\n"]
                    if s.get("customer"):
                        lines.append(f"Customer: {s['customer']}")
                    if s.get("collection"):
                        lines.append(f"Collection: {s['collection']}")
                    if s.get("delivery"):
                        lines.append(f"Delivery: {s['delivery']}")
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
        f"Trailer *{trailer_code}* noted.\n\nWhere did you *collect the load*? Tap one:{hint}",
        parse_mode="Markdown",
        reply_markup=build_keyboard(names, cols=2, extra_row=["Other location"]),
    )
    return COLLECTION


async def job_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "yes" in update.message.text.lower():
        # Collection is known from the job. Skip the collection question.
        # Derive country from the job if we can, otherwise ask it (customs rules need it).
        s = context.user_data.get("job_summary", {})
        from_c = (s.get("from_c") or "").strip()
        mapped = match_country_loose(from_c) if from_c else None
        if from_c.upper() == "EU":
            mapped = None  # corridor-level, need the real country for customs rules

        if mapped:
            context.user_data["country"] = mapped
            # No live position + no geocode yet? Geocode the collection so ETA still works.
            if context.user_data.get("live_lat") is None and context.user_data.get("col_lat") is None:
                col = context.user_data.get("collection")
                if col:
                    lat, lon = geocode(col, mapped)
                    context.user_data["col_lat"], context.user_data["col_lon"] = lat, lon
            return await offer_port(update, context)
        return await ask_country(update, context)

    # Driver says the job is wrong -> drop the matched job and fall back to manual.
    for k in ("job_summary", "suggested_port", "collection", "col_lat", "col_lon", "country"):
        context.user_data.pop(k, None)
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
            f"Found these for *{choice}* - tap one:",
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
    # Matched-job flow -> offer the one-tap port confirm; manual flow -> full port list.
    if context.user_data.get("job_summary"):
        return await offer_port(update, context)
    return await ask_port(update, context)


async def offer_port(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """When we can guess the port from the job/ferry data, offer a one-tap
    confirmation with a 'Different port' fallback. Otherwise show the full list."""
    suggested = context.user_data.get("suggested_port")
    if suggested and suggested in PORTS:
        await update.message.reply_text(
            f"Looks like you're heading to *{suggested}*.\n\nConfirm, or pick a different port:",
            parse_mode="Markdown",
            reply_markup=build_keyboard([f"Yes - {suggested}", "Different port"], cols=1),
        )
        return PORT_CONFIRM
    return await ask_port(update, context)


async def port_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if "different" in text.lower():
        return await ask_port(update, context)
    suggested = context.user_data.get("suggested_port")
    context.user_data["port"] = suggested or match_port(text) or text
    return await proceed_after_port(update, context)


async def ask_port(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Which *port* are you heading to?",
        parse_mode="Markdown",
        reply_markup=build_keyboard(list(PORTS.keys()), cols=2),
    )
    return PORT


async def get_port(update: Update, context: ContextTypes.DEFAULT_TYPE):
    typed = update.message.text.strip()
    context.user_data["port"] = match_port(typed) or typed
    return await proceed_after_port(update, context)


async def proceed_after_port(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["chat_id"] = update.effective_chat.id

    country = context.user_data.get("country", "")
    needs = customs_needed(country, context.user_data["port"])
    context.user_data["needs_customs"] = needs

    if not needs:
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
    job_line = f"Job: {job['job_id']}\n" if job.get("job_id") else ""
    summary = (
        f"*Please check your summary:*\n{status_line}\n"
        f"{job_line}"
        f"Trailer: *{d.get('trailer')}*\n"
        f"Collection: {d.get('collection')} ({d.get('country')})\n"
        f"Port: {d.get('port')}\n"
        f"Distance: {d.get('dist_str')}\n"
        f"Docs: {d.get('docs')}\n"
        f"Notes: {d.get('notes')}"
    )
    await update.message.reply_text(
        summary,
        parse_mode="Markdown",
        reply_markup=build_keyboard(["Send to customs team", "Start again"], cols=1),
    )
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


def main():
    load_address_book()
    print(f"Move IT API: {'ENABLED at ' + MOVEIT_BASE_URL if API.enabled else 'not configured - manual flow only'}")
    app = Application.builder().token(BOT_TOKEN).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start), MessageHandler(filters.TEXT & ~filters.COMMAND, start)],
        states={
            TRAILER:      [MessageHandler(filters.TEXT & ~filters.COMMAND, get_trailer)],
            JOB_CONFIRM:  [MessageHandler(filters.TEXT & ~filters.COMMAND, job_confirm)],
            COLLECTION:   [MessageHandler(filters.TEXT & ~filters.COMMAND, get_collection)],
            COUNTRY:      [MessageHandler(filters.TEXT & ~filters.COMMAND, get_country)],
            PORT:         [MessageHandler(filters.TEXT & ~filters.COMMAND, get_port)],
            PORT_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, port_confirm)],
            DOCS:         [MessageHandler(filters.TEXT | filters.PHOTO | filters.Document.ALL, get_docs)],
            NOTES:        [MessageHandler(filters.TEXT & ~filters.COMMAND, get_notes)],
            CONFIRM:      [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(conv)
    print("Bot v4 is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
