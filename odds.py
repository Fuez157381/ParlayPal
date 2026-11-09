"""Live odds for Parlay Pal (optional). Uses The Odds API (https://the-odds-api.com).

Turn it on: put your API key (one line) in a file named odds_key.txt next to server.py,
or set the ODDS_API_KEY environment variable, then restart server.py.
No key = no odds. Nothing here is ever made up: if a book does not offer a leg, that book
is shown as missing that leg instead of getting a guessed price.
"""
import os
import re
import time
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.environ.get("ODDS_BASE", "https://api.the-odds-api.com")
# Only the apps Parlay Pal shows. Asking by name costs 1 credit per market per game (instead of 1 per region x3).
BOOKS = [b.strip() for b in os.environ.get("ODDS_BOOKS", "fanduel,hardrockbet,prizepicks,underdog,sleeper").split(",") if b.strip()]
SHOWN = set(BOOKS) | {"kalshi", "draftkings"}   # DraftKings comes free from ESPN
REGIONS = "us,us2,us_dfs"                 # sportsbooks, more books (Hard Rock, ESPN BET, Fanatics, Fliff), pick'em apps (Underdog, PrizePicks, Sleeper, Pick6, Betr); each region multiplies the credit cost
TTL = 2 * 3600                             # seconds an event's odds are reused (makes the free 500 credits last)

NAMES = {
    "ATL": "Atlanta Hawks", "BOS": "Boston Celtics", "BKN": "Brooklyn Nets", "CHA": "Charlotte Hornets",
    "CHI": "Chicago Bulls", "CLE": "Cleveland Cavaliers", "DAL": "Dallas Mavericks", "DEN": "Denver Nuggets",
    "DET": "Detroit Pistons", "GSW": "Golden State Warriors", "HOU": "Houston Rockets", "IND": "Indiana Pacers",
    "LAC": "Los Angeles Clippers", "LAL": "Los Angeles Lakers", "MEM": "Memphis Grizzlies", "MIA": "Miami Heat",
    "MIL": "Milwaukee Bucks", "MIN": "Minnesota Timberwolves", "NOP": "New Orleans Pelicans", "NYK": "New York Knicks",
    "OKC": "Oklahoma City Thunder", "ORL": "Orlando Magic", "PHI": "Philadelphia 76ers", "PHX": "Phoenix Suns",
    "POR": "Portland Trail Blazers", "SAC": "Sacramento Kings", "SAS": "San Antonio Spurs", "TOR": "Toronto Raptors",
    "UTA": "Utah Jazz", "WAS": "Washington Wizards",
}
MARKET = {"pts": "player_points", "reb": "player_rebounds", "ast": "player_assists", "tpm": "player_threes"}

_cache = {}   # (event_id, markets) -> (time, json)
_events = {"t": 0, "v": None}


def api_key():
    p = os.path.join(HERE, "odds_key.txt")
    if os.path.exists(p):
        with open(p, encoding="utf-8-sig") as f:
            k = f.read().strip().strip('"').strip("'")
        if k:
            return k
    return os.environ.get("ODDS_API_KEY", "").strip()


def status():
    k = api_key()
    out = {"configured": bool(k), "keyEnds": k[-4:] if k else ""}
    if k:
        try:   # listing sports is free and reports this month's credits
            r = _get("/v4/sports", {"apiKey": k})
            out.update(httpStatus=r.status_code, creditsLeft=r.headers.get("x-requests-remaining"), creditsUsed=r.headers.get("x-requests-used"))
        except Exception as e:
            out["checkError"] = type(e).__name__
    return out


def norm(s):
    s = unicodedata.normalize("NFD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"[’'.]", "", s).replace("-", " ")
    s = re.sub(r"\b(jr|sr|ii|iii|iv)\b", "", s)
    return re.sub(r"\s+", " ", s).strip()


def to_decimal(a):
    return 1 + a / 100.0 if a > 0 else 1 + 100.0 / -a


def to_american(d):
    if d <= 1:
        return None
    return int(round((d - 1) * 100)) if d >= 2 else int(round(-100 / (d - 1)))


def _get(path, params):
    import requests
    r = requests.get(BASE + path, params=params, timeout=20)
    return r


def _events_list(key):
    if _events["v"] is not None and time.time() - _events["t"] < TTL:
        return _events["v"]
    import kv
    shared = kv.get("oddsapi:events", TTL)
    if shared is not None:
        _events["v"], _events["t"] = shared, time.time()
        return shared
    r = _get("/v4/sports/basketball_nba/events", {"apiKey": key, "dateFormat": "iso"})
    if r.status_code != 200:
        raise RuntimeError(f"odds provider said {r.status_code}: {r.text[:120]}")
    _events["v"], _events["t"] = r.json(), time.time()
    kv.put("oddsapi:events", _events["v"])
    return _events["v"]


def _event_odds(key, eid, markets):
    ck = (eid, ",".join(sorted(markets)) + "|" + ",".join(BOOKS))
    hit = _cache.get(ck)
    if hit and time.time() - hit[0] < TTL:
        return hit[1], None
    import kv
    shared = kv.get("oddsapi:" + ck[0] + ":" + ck[1], TTL)
    if shared is not None:
        _cache[ck] = (time.time(), shared)
        return shared, None
    params = {"apiKey": key, "bookmakers": ",".join(BOOKS), "markets": ",".join(sorted(markets)), "oddsFormat": "american"}
    r = _get(f"/v4/sports/basketball_nba/events/{eid}/odds", params)
    if r.status_code == 422:                       # the plan refused the app list: fall back to regions
        params.pop("bookmakers", None)
        params["regions"] = REGIONS
        r = _get(f"/v4/sports/basketball_nba/events/{eid}/odds", params)
    if r.status_code != 200:
        raise RuntimeError(f"odds provider said {r.status_code}: {r.text[:120]}")
    _cache[ck] = (time.time(), r.json())
    kv.put("oddsapi:" + ck[0] + ":" + ck[1], r.json())
    return r.json(), r.headers.get("x-requests-remaining")


def leg_price(event_json, leg):
    """{bookKey: (title, american)} for 'over bar-0.5' on this player's stat, only where a book really lists it."""
    out = {}
    want_pt = float(leg["bar"]) - 0.5
    base = MARKET[leg["s"]]
    names = (base, base + "_alternate")
    who = norm(leg["n"])
    for b in event_json.get("bookmakers", []):
        for m in b.get("markets", []):
            if m.get("key") not in names:
                continue
            for o in m.get("outcomes", []):
                if str(o.get("name", "")).lower() != "over" or norm(o.get("description", "")) != who:
                    continue
                if o.get("point") is None or abs(float(o["point"]) - want_pt) > 1e-6 or o.get("price") is None:
                    continue
                out.setdefault(b["key"], (b.get("title", b["key"]), int(o["price"])))
    return out


def compare(legs):
    """Prices from every free source we have, merged per book: SportsGameOdds (free plan, 9 sportsbooks),
    The Odds API (only while its credits last), and Kalshi (always free). A book's price for a leg comes
    from the first source that has it."""
    legs = [l for l in legs if l.get("s") and l.get("bar") and l.get("n") and l.get("t") and l.get("opp")][:20]   # every leg counts: a book missing one is shown as missing it
    if not legs:
        return {"configured": True, "books": [], "notes": ["Add legs to see prices."]}
    import kalshi
    import sgo
    per_leg = [{} for _ in legs]
    notes, used, remaining = [], [], None

    def merge(rows, name):
        if any(rows):
            used.append(name)
        for d, r in zip(per_leg, rows):
            for k, v in r.items():
                if k in SHOWN:             # only the apps Parlay Pal shows
                    d.setdefault(k, v)

    if sgo.api_key():
        try:
            merge(sgo.leg_prices(legs), "SportsGameOdds")
        except Exception as e:
            notes.append(f"SportsGameOdds didn't answer ({type(e).__name__}: {str(e)[:140]}).")
    dk = []
    try:
        import espn_props
        merge(espn_props.leg_prices(legs), "DraftKings via ESPN")
        dk = espn_props.main_lines(legs)
    except Exception as e:
        notes.append(f"ESPN's DraftKings prices didn't answer ({type(e).__name__}).")
    if api_key():
        try:
            rows, _, remaining = _oddsapi_prices(api_key(), legs)
            merge(rows, "The Odds API")
        except Exception:
            pass   # usually out of credits: the other sources still work
    try:
        rows = [{"kalshi": ("Kalshi", r["american"])} if r.get("found") and r.get("american") is not None else {}
                for r in kalshi.match(legs)]
        merge(rows, "Kalshi")
    except Exception as e:
        notes.append(f"Kalshi didn't answer ({type(e).__name__}).")
    books = _books(legs, per_leg)
    if used:
        notes.append("Prices from " + ", ".join(used) + "." + (" Sportsbook prices can be a few minutes old, so check the app before you bet." if "SportsGameOdds" in used else ""))
    notes.append("Parlay price is each leg's listed price multiplied together. A book's own slip can price a parlay differently, and some books limit same-game parlays.")
    return {"configured": True, "updated": time.strftime("%H:%M"), "books": books, "notes": notes, "creditsLeft": remaining, "dk": dk}


def _oddsapi_prices(key, legs):
    events = _events_list(key)
    by_teams = {frozenset((e["home_team"], e["away_team"])): e for e in events}
    notes, remaining = [], None
    per_leg = []                                   # one dict per leg: bookKey -> (title, american)
    ev_cache = {}
    for l in legs:
        if l.get("s") not in MARKET:               # stats The Odds API doesn't carry (double-doubles, NFL)
            per_leg.append({})
            continue
        ev = by_teams.get(frozenset((NAMES.get(l["t"], ""), NAMES.get(l["opp"], ""))))
        if not ev:
            per_leg.append({})
            notes.append(f"{l['n']}: the odds provider has no game for {l['t']} vs {l['opp']}.")
            continue
        if ev["id"] not in ev_cache:
            mk = set()
            for x in legs:
                e2 = by_teams.get(frozenset((NAMES.get(x["t"], ""), NAMES.get(x["opp"], ""))))
                if e2 and e2["id"] == ev["id"] and x.get("s") in MARKET:
                    mk.add(MARKET[x["s"]]); mk.add(MARKET[x["s"]] + "_alternate")
            data, rem = _event_odds(key, ev["id"], mk)
            ev_cache[ev["id"]] = data
            remaining = rem or remaining
        per_leg.append(leg_price(ev_cache[ev["id"]], l))
    return per_leg, notes, remaining


def _books(legs, per_leg):
    titles = {}
    for d in per_leg:
        for k, (t, _) in d.items():
            titles[k] = t
    books = []
    for k, t in titles.items():
        prices = [d.get(k, (None, None))[1] for d in per_leg]
        got = [p for p in prices if p is not None]
        row = {"key": k, "title": t, "legsOffered": len(got), "legsTotal": len(legs), "legs": prices,
               "missing": [legs[i]["n"] for i, p in enumerate(prices) if p is None], "american": None}
        if len(got) == len(legs):
            dec = 1.0
            for p in got:
                dec *= to_decimal(p)
            row["decimal"] = round(dec, 4)
            row["american"] = to_american(dec)
        books.append(row)
    books.sort(key=lambda b: (b["american"] is None, -(b.get("decimal") or 0), -b["legsOffered"], b["title"]))
    return books


# ---------------------------------------------------------------------------------------------
# Which lines do apps really list?  (Parlay Pal only lets you pick lines that at least one app offers.)
# Costs credits: markets x regions per game (8 x 4 = 32 per game). The answer is saved to
# lines_cache.json and reused for LINES_TTL seconds, so restarting the server does not spend more.
# ---------------------------------------------------------------------------------------------
import json

LINES_FILE = os.path.join(HERE, "lines_cache.json")
LINES_TTL = 24 * 3600
_STAT_OF = {}
for _s, _m in MARKET.items():
    _STAT_OF[_m] = _s
    _STAT_OF[_m + "_alternate"] = _s


def _load_lines():
    try:
        with open(LINES_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_lines(d):
    now = time.time()
    d = {k: v for k, v in d.items() if now - v.get("t", 0) < 3 * 86400}
    try:
        with open(LINES_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f)
    except OSError:
        pass


def extract_bars(event_json):
    """{'player name|stat': [bar, ...]} where bar = the smallest whole number that clears an Over line."""
    out = {}
    for b in event_json.get("bookmakers", []):
        for m in b.get("markets", []):
            st = _STAT_OF.get(m.get("key"))
            if not st:
                continue
            for o in m.get("outcomes", []):
                if str(o.get("name", "")).lower() != "over" or o.get("point") is None:
                    continue
                who = norm(o.get("description", ""))
                if not who:
                    continue
                out.setdefault(f"{who}|{st}", set()).add(int(float(o["point"]) // 1) + 1)
    return {k: sorted(v) for k, v in out.items()}


def lines(games):
    key = api_key()
    # Loading every game's lines (with alternates) costs ~24 credits per game, far too much for the free 500.
    # Kalshi already tells the page which lines exist, so this stays off unless ODDS_LINES=1 (a paid plan).
    if not key or os.environ.get("ODDS_LINES", "").strip() != "1":
        return {"configured": False}
    events = _events_list(key)
    by_teams = {frozenset((e["home_team"], e["away_team"])): e for e in events}
    disk = _load_lines()
    players, posted, remaining, changed = {}, {}, None, False
    allm = set(MARKET.values()) | {m + "_alternate" for m in MARKET.values()}
    for g in (games or [])[:16]:
        a, h = g.get("a"), g.get("h")
        ev = by_teams.get(frozenset((NAMES.get(a, ""), NAMES.get(h, ""))))
        if not ev:
            posted[a] = posted[h] = False
            continue
        ent = disk.get(ev["id"])
        if not ent or time.time() - ent.get("t", 0) > LINES_TTL or (not ent.get("bars") and time.time() - ent.get("t", 0) > 1800):
            data, rem = _event_odds(key, ev["id"], allm)
            ent = {"t": time.time(), "bars": extract_bars(data)}
            disk[ev["id"]] = ent
            changed = True
            remaining = rem or remaining
        posted[a] = posted[h] = bool(ent["bars"])
        players.update(ent["bars"])
    if changed:
        _save_lines(disk)
    return {"configured": True, "updated": time.strftime("%H:%M"), "players": players, "posted": posted, "creditsLeft": remaining}
