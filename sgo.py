"""SportsGameOdds (https://sportsgameodds.com) for Parlay Pal. Free "Amateur" plan: 9 sportsbooks
(FanDuel, DraftKings, BetMGM, Caesars, ESPN BET, Bovada, Unibet, PointsBet, William Hill), player props
included, data refreshed about every 10 minutes.

Turn it on: put your key (one line) in sgo_key.txt next to server.py, or set the SGO_API_KEY env var.
The free plan allows 2,500 "objects" a month, and every game returned counts as one object (no matter
how many props or books it has). So today's games are fetched once and reused for TTL seconds.
Nothing is guessed: a book that does not list a leg is shown as missing that leg.
"""
import datetime as dt
import json
import os
import time

from odds import NAMES, norm

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = "https://api.sportsgameodds.com/v2"
TTL = int(os.environ.get("SGO_TTL", 4 * 3600))      # 4 hours keeps a normal month under the free 2,500 games
STAT = {"pts": "points", "reb": "rebounds", "ast": "assists", "tpm": "threePointersMade"}
# SportsGameOdds bookmaker ids -> the keys the site already knows (so the right logo and link show)
BOOK_KEY = {"caesars": "williamhill_us", "williamhill": "williamhill_us", "hardrock": "hardrockbet"}
BOOK_TITLE = {"draftkings": "DraftKings", "fanduel": "FanDuel", "betmgm": "BetMGM", "williamhill_us": "Caesars",
              "espnbet": "ESPN BET", "bovada": "Bovada", "unibet": "Unibet", "pointsbet": "PointsBet",
              "hardrockbet": "Hard Rock", "fanatics": "Fanatics", "betrivers": "BetRivers", "bet365": "bet365",
              "underdog": "Underdog", "prizepicks": "PrizePicks", "sleeper": "Sleeper", "fliff": "Fliff", "ballybet": "Bally Bet"}
CACHE_FILE = "/tmp/sgo_cache.json" if os.environ.get("VERCEL") else os.path.join(HERE, "sgo_cache.json")
_mem = {"t": 0, "v": None}


def api_key():
    p = os.path.join(HERE, "sgo_key.txt")
    if os.path.exists(p):
        with open(p, encoding="utf-8-sig") as f:
            k = f.read().strip().strip('"').strip("'")
        if k:
            return k
    return os.environ.get("SGO_API_KEY", "").strip()


def _num(v):
    try:
        return float(str(v).replace("+", ""))
    except (TypeError, ValueError):
        return None


def _fetch(key):
    import requests
    now = dt.datetime.now(dt.timezone.utc)
    params = {"leagueID": "NBA", "oddsAvailable": "true", "includeAltLines": "true", "limit": 50,
              "startsAfter": (now - dt.timedelta(hours=4)).isoformat(), "startsBefore": (now + dt.timedelta(hours=36)).isoformat()}
    out, cursor = [], None
    for _ in range(3):
        if cursor:
            params["cursor"] = cursor
        r = requests.get(BASE + "/events/", params=params, headers={"X-Api-Key": key}, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"SportsGameOdds said {r.status_code}: {r.text[:120]}")
        j = r.json()
        out += j.get("data") or []
        cursor = j.get("nextCursor")
        if not cursor:
            break
    return out


def _shared_get():
    """The last fetch, kept in the accounts database so every server copy shares one cache."""
    try:
        import auth
        if not auth.pg_url():
            return None
        with auth.db() as con:
            con.execute("CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, t BIGINT NOT NULL, v TEXT NOT NULL)")
            r = con.execute("SELECT t, v FROM kv WHERE k=?", ("sgo",)).fetchone()
            return {"t": r["t"], **json.loads(r["v"])} if r else None
    except Exception:
        return None


def _shared_put(rec):
    try:
        import auth
        if not auth.pg_url():
            return
        with auth.db() as con:
            con.execute("CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, t BIGINT NOT NULL, v TEXT NOT NULL)")
            con.execute("DELETE FROM kv WHERE k=?", ("sgo",))
            con.execute("INSERT INTO kv(k, t, v) VALUES(?,?,?)", ("sgo", int(rec["t"]), json.dumps({k: v for k, v in rec.items() if k != "t"})))
    except Exception:
        pass


BACKOFF = 600   # after "too many requests", wait 10 minutes before asking again


def events():
    key = api_key()
    if not key:
        return None
    now = time.time()
    if _mem["v"] is not None and now - _mem["t"] < TTL:
        return _mem["v"]
    rec = _shared_get()
    if rec is None:
        try:   # a saved copy survives restarts
            with open(CACHE_FILE, encoding="utf-8") as f:
                rec = json.load(f)
        except (OSError, ValueError):
            rec = None
    if rec and rec.get("v") is not None and now - rec.get("t", 0) < TTL:
        _mem.update(t=rec["t"], v=rec["v"])
        return _mem["v"]
    if rec and now - rec.get("failT", 0) < BACKOFF:     # failed recently: use the last good prices, or give up quietly
        if rec.get("v") is not None:
            return rec["v"]
        raise RuntimeError("SportsGameOdds is cooling down after a rate limit. Trying again in a few minutes.")
    try:
        evs = _fetch(key)
    except RuntimeError:
        bad = dict(rec or {}, failT=now)
        bad.setdefault("t", 0)
        _shared_put(bad)
        if rec and rec.get("v") is not None:
            return rec["v"]
        raise
    new = {"t": now, "v": evs}
    _mem.update(t=now, v=evs)
    _shared_put(new)
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(new, f)
    except OSError:
        pass
    return evs


def _team_text(ev):
    parts = []
    for side in ("home", "away"):
        t = (ev.get("teams") or {}).get(side) or {}
        parts += [str(v) for v in (t.get("names") or {}).values()]
        parts.append(str(t.get("teamID", "")).replace("_", " "))
    return norm(" ".join(parts))


def find_event(evs, a, b):
    for ev in evs:
        txt = _team_text(ev)
        if all(norm(NAMES.get(x, "")) and norm(NAMES[x]) in txt for x in (a, b)):
            return ev
    return None


def _player_id(ev, name):
    who = norm(name)
    for pid, p in (ev.get("players") or {}).items():
        full = p.get("name") or f"{p.get('firstName', '')} {p.get('lastName', '')}"
        if norm(full) == who:
            return pid
    return None


def _lines(book):
    """Every (overUnder, american odds) a book lists for one market, main line plus alternates."""
    rows = [book] + [x for x in (book.get("altLines") or []) if isinstance(x, dict)]
    out = []
    for x in rows:
        if x.get("available") is False:
            continue
        ou, am = _num(x.get("overUnder")), _num(x.get("odds"))
        if ou is not None and am is not None:
            out.append((ou, int(am)))
    return out


def leg_prices(legs):
    """One dict per leg: bookKey -> (title, american) for 'over bar-0.5', only where a book really lists it."""
    evs = events() or []
    res = []
    for l in legs:
        got = {}
        ev = find_event(evs, l.get("t"), l.get("opp")) if l.get("s") in STAT else None
        pid = _player_id(ev, l.get("n", "")) if ev else None
        odd = (ev.get("odds") or {}).get(f"{STAT[l['s']]}-{pid}-game-ou-over") if pid else None
        if odd:
            want = float(l["bar"]) - 0.5
            for bid, book in (odd.get("byBookmaker") or {}).items():
                k = BOOK_KEY.get(bid, bid)
                for ou, am in _lines(book):
                    if abs(ou - want) < 1e-6:
                        got.setdefault(k, (BOOK_TITLE.get(k, bid.title()), am))
        res.append(got)
    return res


def lines(games):
    """{'player name|stat': [bar, ...]} for today's games, from the same saved data (costs nothing extra)."""
    evs = events() or []
    players, posted = {}, {}
    for g in (games or [])[:16]:
        a, h = str(g.get("a", "")).upper(), str(g.get("h", "")).upper()
        ev = find_event(evs, a, h)
        found = False
        for odd in ((ev or {}).get("odds") or {}).values():
            st = next((k for k, v in STAT.items() if v == odd.get("statID")), None)
            p = (ev.get("players") or {}).get(odd.get("statEntityID"))
            if not st or not p or odd.get("periodID") != "game" or odd.get("betTypeID") != "ou" or odd.get("sideID") != "over":
                continue
            who = norm(p.get("name") or f"{p.get('firstName', '')} {p.get('lastName', '')}")
            for book in (odd.get("byBookmaker") or {}).values():
                for ou, _ in _lines(book):
                    players.setdefault(f"{who}|{st}", set()).add(int(ou // 1) + 1)
                    found = True
        posted[a] = posted[h] = found
    return {"players": players, "posted": posted}


def status():
    k = api_key()
    return {"configured": bool(k), "keyEnds": k[-4:] if k else "",
            "cachedGames": len(_mem["v"] or []), "cachedAt": time.strftime("%H:%M", time.localtime(_mem["t"])) if _mem["t"] else ""}
