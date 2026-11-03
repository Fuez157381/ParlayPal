"""PropCart data service (free, runs on your own computer).

Setup:   pip install -r requirements.txt
Check:   python server.py --check     (prints games / teams / players / game logs loaded)
Run:     python server.py             (then open http://localhost:8000)

Data comes from ESPN's free public NBA API (espn.py): schedule, game logs, injuries and live box scores.
The NBA's own sites block cloud servers, ESPN doesn't. Nothing is made up: if ESPN can't be reached
you get an error, never sample numbers. Responses are cached in cache.sqlite so repeat loads are quick.
"""
import json
import os
import re
import sqlite3
import sys
import time
import unicodedata
import datetime as dt
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, request, send_file

HERE = os.path.dirname(os.path.abspath(__file__))
HOSTED = bool(os.environ.get("PORT") or os.environ.get("VERCEL"))
# Vercel's disk is read-only except /tmp
DB = "/tmp/cache.sqlite" if os.environ.get("VERCEL") else os.path.join(HERE, "cache.sqlite")
OLD_PAGE = os.path.join(HERE, "propcart_live.html")
PAGE = os.path.join(HERE, "parlaypal.html")
TTL_SCOREBOARD = 10 * 60        # seconds
TTL_LOGS = 6 * 60 * 60
TTL_SCHEDULE = 3 * 60 * 60
ESPN_INJ = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries"
TTL_INJ = 20 * 60               # injury report refreshed every 20 minutes
MAX_GAMES = 20                  # per player, newest first (previous season fills gaps)

app = Flask(__name__)
import auth  # noqa: E402
app.register_blueprint(auth.bp, url_prefix="/api/auth")


# ---------- cache ----------
def cached(key, ttl, fn):
    """Return (data, source) where source is 'cache', 'live' or 'stale-cache'."""
    con = sqlite3.connect(DB, timeout=60)  # several downloads can save at once; wait instead of failing
    con.execute("create table if not exists cache(k text primary key, ts real, body text)")
    row = con.execute("select ts, body from cache where k=?", (key,)).fetchone()
    if row and time.time() - row[0] < ttl:
        con.close()
        return json.loads(row[1]), "cache"
    try:
        data = fn()
    except Exception:
        con.close()
        if row:  # NBA unreachable: fall back to the last real download, clearly labeled
            return json.loads(row[1]), "stale-cache"
        raise
    con.execute("insert or replace into cache values(?,?,?)", (key, time.time(), json.dumps(data)))
    con.commit()
    con.close()
    return data, "live"


BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*", "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.nba.com", "Referer": "https://www.nba.com/", "Connection": "keep-alive",
}


def norm_name(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z ]", " ", s)
    return " ".join(w for w in s.split() if w not in ("jr", "sr", "ii", "iii", "iv", "v"))


def inj_level(status):
    st = (status or "").lower()
    if "out" in st or "doubtful" in st or "suspen" in st:
        return "out"
    if "day" in st or "question" in st:
        return "q"
    return None


def fetch_injuries():
    """ESPN's public NBA injury report: {normalized name: {s: out|q, t: text}}."""
    import requests
    r = requests.get(ESPN_INJ, timeout=20, headers=BROWSER_HEADERS)
    r.raise_for_status()
    out = {}
    for team in r.json().get("injuries", []):
        for it in team.get("injuries", []):
            ath = it.get("athlete") or {}
            name = ath.get("displayName") or ath.get("fullName")
            st = it.get("status") or (it.get("type") or {}).get("description") or ""
            lvl = inj_level(st)
            if not name or not lvl:
                continue
            det = it.get("details") or {}
            why = (it.get("shortComment") or it.get("longComment") or " ".join(x for x in (det.get("type"), det.get("location")) if x) or "").strip()
            label = "Out" if lvl == "out" else "Questionable"
            out[norm_name(name)] = {"s": lvl, "t": (why if why.lower().startswith(label.lower()) else label + (": " + why if why else ""))[:170], "src": "ESPN"}
    return out


def games_for(date_str):
    """Games on one date (YYYY-MM-DD, Eastern). No date = today. Returns (games, dates_with_games, date_str, source)."""
    et = ZoneInfo("America/New_York")
    today = dt.datetime.now(et).strftime("%Y-%m-%d")
    date_str = date_str or today
    import espn
    start = int(season_str(0)[:4])
    # today's games change status during the night, so today's schedule is refreshed more often
    sched, src = cached(f"espn:schedule:{start}", TTL_SCOREBOARD if date_str == today else TTL_SCHEDULE,
                        lambda: espn.fetch_schedule(start))
    rows, dates = sched.get(date_str, []), sorted(sched)
    games = []
    for i, g in enumerate(rows, 1):
        try:
            when = dt.datetime.fromisoformat(g["utc"].replace("Z", "+00:00")).astimezone(et)
            label = f"{when.hour % 12 or 12}:{when:%M} {'AM' if when.hour < 12 else 'PM'}"
        except ValueError:
            label = g["status"] or "TBD"
        if "final" in g["status"].lower():
            label = "Final"
        y, m, d = date_str.split("-")
        day = dt.date(int(y), int(m), int(d))
        games.append({"i": i, "id": g["id"], "a": g["a"], "h": g["h"],
                      "d": f"{day:%a %b} {day.day}", "t": label})
    return games, dates, date_str, src


def season_str(offset=0):
    today = dt.datetime.now(ZoneInfo("America/New_York"))
    start = today.year if today.month >= 10 else today.year - 1
    start -= offset
    return f"{start}-{str(start + 1)[2:]}"


# ---------- transform ----------
def opponent_factors(cur):
    """How much each team allows vs the league average, from real game totals this season."""
    tot = {}
    for r in cur:
        t = tot.setdefault((r["gid"], r["team"]), {"pts": 0, "reb": 0, "ast": 0, "tpa": 0})
        for k in t:
            t[k] += r[k]
    by_game = {}
    for (gid, team), t in tot.items():
        by_game.setdefault(gid, {})[team] = t
    allowed, league, margins = {}, {"pts": [], "reb": [], "ast": [], "tpa": []}, {}
    for gid, teams in by_game.items():
        if len(teams) != 2:
            continue
        (a, ta), (b, tb) = teams.items()
        for me, mine, opp in ((a, ta, tb), (b, tb, ta)):
            allowed.setdefault(me, []).append(opp)
            margins.setdefault(me, []).append(mine["pts"] - opp["pts"])
            for k in league:
                league[k].append(opp[k])
    avg = {k: sum(v) / len(v) for k, v in league.items() if v}
    opp, why = {}, {}
    names = {"pts": "points", "reb": "rebounds", "ast": "assists", "tpa": "three-point attempts"}
    for team, rows in allowed.items():
        n = len(rows)
        m = {k: sum(r[k] for r in rows) / n for k in avg}
        opp[team] = {"pts": round(m["pts"] / avg["pts"], 2), "reb": round(m["reb"] / avg["reb"], 2),
                     "ast": round(m["ast"] / avg["ast"], 2), "tpm": round(m["tpa"] / avg["tpa"], 2)}
        why[team] = {
            key: f"{team} has allowed {m[src]:.1f} {names[src]} per game over {n} game{'s' if n != 1 else ''} "
                 f"(league average {avg[src]:.1f})."
            for key, src in (("pts", "pts"), ("reb", "reb"), ("ast", "ast"), ("tpm", "tpa"))
        }
    net = {t: {"m": round(sum(v) / len(v), 1), "g": len(v)} for t, v in margins.items()}
    return opp, why, net


def build(date_str=None):
    et = ZoneInfo("America/New_York")
    games, dates, date_str, sb_src = games_for(date_str)
    teams = {t for g in games for t in (g["a"], g["h"])}

    import espn
    year = int(season_str(0)[:4]) + 1          # ESPN names a season by the year it ends: 2026-27 is 2027
    roster, _ = cached("espn:rosters", TTL_LOGS, espn.fetch_rosters)
    cur = espn.all_logs(roster, year, cached, TTL_LOGS)
    prev = espn.all_logs(roster, year - 1, cached, 30 * 24 * 3600)   # last season never changes
    cur_src = prev_src = "espn"
    if not cur:
        raise RuntimeError("ESPN returned no game logs for this season")

    def by_player(rows):
        d = {}
        for r in rows:
            d.setdefault(r["pid"], []).append(r)
        for v in d.values():
            v.sort(key=lambda r: r["date"], reverse=True)
        return d

    cp, pp = by_player(cur), by_player(prev)
    players, cur_logs, prev_logs = [], 0, 0
    for pid, rows in cp.items():
        team = (roster.get(str(pid)) or {}).get("t") or rows[0]["team"]   # current roster team (handles trades)
        if team not in teams:
            continue
        series = rows[:MAX_GAMES]
        extra = pp.get(pid, [])[: MAX_GAMES - len(series)]
        allg = series + extra
        cur_logs += len(series)
        prev_logs += len(extra)
        players.append({"id": pid, "n": rows[0]["name"], "t": team, "ncur": len(series),
                        "pts": [r["pts"] for r in allg], "reb": [r["reb"] for r in allg],
                        "ast": [r["ast"] for r in allg], "tpm": [r["tpm"] for r in allg],
                        "min": [r["min"] for r in allg], "dates": [r["date"] for r in allg],
                        "mu": [r.get("mu", "") for r in allg]})
    try:
        inj_map, inj_src = cached("injuries", TTL_INJ, fetch_injuries)
    except Exception as e:
        inj_map, inj_src = {}, "unavailable"
        print("injury report unavailable:", type(e).__name__, e)
    # second check that needs no outside source: did he play in his team's most recent games?
    tdates = {}
    for rows in cp.values():
        for r in rows:
            tdates.setdefault(r["team"], set()).add(r["date"])
    for pl in players:
        pid = pl["id"]
        hit = inj_map.get(norm_name(pl["n"]))
        if hit:
            pl["inj"] = hit
            continue
        td = sorted(tdates.get(pl["t"], ()), reverse=True)
        if len(td) >= 4:
            mine = {r["date"] for r in cp[pid]}
            missed = [d for d in td[:3] if d not in mine]
            if len(missed) >= 2:
                pl["inj"] = {"s": "out", "t": "Likely out: missed %d of his team's last 3 games." % len(missed), "src": "games"}
            elif td[0] not in mine:
                pl["inj"] = {"s": "q", "t": "Missed his team's most recent game.", "src": "games"}
    opp, why, net = opponent_factors(cur)
    return {
        "games": games, "players": players, "opp": opp, "why": why, "net": net, "dates": dates,
        "stats": {"date": date_str, "today": dt.datetime.now(et).strftime("%Y-%m-%d"), "games": len(games), "teams": len(teams), "players": len(players),
                  "logs_current_season": cur_logs, "logs_previous_season": prev_logs,
                  "injuries": {"source": inj_src, "listed": len(inj_map), "hidden": sum(1 for pl in players if pl.get("inj", {}).get("s") == "out")},
                  "sources": {"scoreboard": sb_src, "current_logs": cur_src, "previous_logs": prev_src},
                  "updated": dt.datetime.now(et).isoformat(timespec="seconds")},
    }


# ---------- web ----------
def cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


@app.route("/api/data")
def api_data():
    date = request.args.get("date", "").strip()
    if date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        return cors(jsonify({"error": "date must look like 2026-11-02"})), 400
    if HOSTED:  # hosted: use today's saved copy right away instead of waiting on a blocked NBA
        snap = load_snapshot(date)
        today = dt.datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
        if snap and snap.get("stats", {}).get("today") == today:
            return cors(jsonify(snap))
        if os.environ.get("VERCEL"):  # the NBA blocks Vercel, so a live call only hangs until the 60 s limit
            if snap:
                return cors(jsonify(snap))  # an older saved copy beats endless loading
            return cors(jsonify({"error": "This day's data hasn't been saved yet. It refreshes overnight, so check back soon."})), 503
    try:
        return cors(jsonify(build(date or None)))
    except Exception as e:
        # live NBA data failed (some hosts get blocked): serve the saved copy if there is one
        snap = load_snapshot(date)
        if snap:
            return cors(jsonify(snap))
        return cors(jsonify({"error": f"{type(e).__name__}: {e}"})), 502


def load_snapshot(date):
    d = os.path.join(HERE, "snapshots")
    for name in ([f"{date}.json"] if date else ["latest.json"]):
        p = os.path.join(d, name)
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    j = json.load(f)
                j.setdefault("stats", {})["snapshot"] = True
                return j
            except Exception:
                pass
    return None


@app.route("/api/feedback", methods=["POST"])
def api_feedback():
    body = request.get_json(silent=True) or {}
    row = {"time": dt.datetime.now().isoformat(timespec="seconds"), "stars": body.get("stars"), "text": str(body.get("text", ""))[:500]}
    try:
        with open(os.path.join(HERE, "feedback.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except OSError:
        pass
    return cors(jsonify({"ok": True}))


@app.route("/api/kalshi/probe")
def api_kalshi_probe():
    import kalshi
    return cors(jsonify(kalshi.probe()))


@app.route("/api/kalshi", methods=["POST"])
def api_kalshi():
    import kalshi
    body = request.get_json(silent=True) or {}
    try:
        return cors(jsonify({"legs": kalshi.match((body.get("legs") or [])[:20])}))
    except Exception as e:
        return cors(jsonify({"error": f"{type(e).__name__}: {e}"})), 502


@app.route("/api/lines", methods=["POST"])
def api_lines():
    import kalshi   # free: Kalshi market data needs no key and uses no Odds API credits
    import sgo      # free plan: reuses the saved SportsGameOdds data, costs nothing extra
    body = request.get_json(silent=True) or {}
    games = body.get("games") or []
    out, errs = None, []
    try:
        out = kalshi.lines(games)
    except Exception as e:
        errs.append(f"Kalshi: {type(e).__name__}: {e}")
    if sgo.api_key():
        try:
            more = sgo.lines(games)
            if out is None:
                out = {"configured": True, "updated": time.strftime("%H:%M"), "players": {}, "posted": {}, "creditsLeft": None}
            for k, bars in more["players"].items():
                out["players"][k] = sorted(set(out["players"].get(k, [])) | bars)
            for t, ok in more["posted"].items():
                out["posted"][t] = bool(out["posted"].get(t)) or ok
            out["source"] = "Kalshi + sportsbooks" if not errs else "sportsbooks"
        except Exception as e:
            errs.append(f"SportsGameOdds: {type(e).__name__}: {e}")
    if out is None:
        return cors(jsonify({"configured": True, "error": "; ".join(errs)})), 502
    return cors(jsonify(out))


def day_data(date):
    """The same day's data the page shows, for Pal to look players up in."""
    date = str(date or "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        date = ""
    if HOSTED:
        snap = load_snapshot(date)
        if snap:
            return snap
    try:
        return build(date or None)
    except Exception:
        return load_snapshot(date) or {}


@app.route("/api/ask", methods=["POST"])
def api_ask():
    import ask
    body = request.get_json(silent=True) or {}
    context = body.get("context") or {}
    try:
        return cors(jsonify(ask.ask(body.get("message", ""), body.get("history"), context, lambda: day_data(context.get("date")))))
    except Exception as e:
        return cors(jsonify({"configured": bool(ask.api_key()), "error": f"{type(e).__name__}: {e}"})), 502


@app.route("/api/polymarket/probe")
def api_polymarket_probe():
    import polymarket
    try:
        return cors(jsonify(polymarket.probe()))
    except Exception as e:
        return cors(jsonify({"error": f"{type(e).__name__}: {e}"})), 502


@app.route("/api/live", methods=["POST"])
def api_live():
    import live
    body = request.get_json(silent=True) or {}
    try:
        return cors(jsonify({"games": live.many([str(g) for g in (body.get("gids") or [])])}))
    except Exception as e:
        return cors(jsonify({"error": f"{type(e).__name__}: {e}"})), 502


@app.route("/api/odds/status")
def api_odds_status():
    import odds
    import sgo
    return cors(jsonify(dict(odds.status(), sportsgameodds=sgo.status())))


@app.route("/api/odds", methods=["POST"])
def api_odds():
    import odds
    body = request.get_json(silent=True) or {}
    legs = body.get("legs") or []
    try:
        return cors(jsonify(odds.compare(legs)))
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        # odds key out of credits (or bad): fall back to Kalshi's free public prices, real data only
        try:
            return cors(jsonify(kalshi_only(legs, err)))
        except Exception as e2:
            return cors(jsonify({"configured": bool(odds.api_key()), "error": f"{err} (Kalshi backup also failed: {type(e2).__name__})"})), 502


def kalshi_only(legs, why):
    import kalshi
    import odds
    res = kalshi.match(legs[:20])
    prices, missing, dec = [], [], 1.0
    for l, r in zip(legs, res):
        am = r.get("american") if r.get("found") else None
        prices.append(am)
        if am is None:
            missing.append(f"{l.get('n')} {l.get('bar')}+")
        else:
            dec *= odds.to_decimal(am)
    n_off = len(legs) - len(missing)
    book = {"key": "kalshi", "title": "Kalshi", "legs": prices, "legsOffered": n_off, "legsTotal": len(legs), "missing": missing}
    if legs and not missing:
        book["decimal"] = round(dec, 4)
        book["american"] = odds.to_american(dec)
    return {"configured": True, "books": [book], "updated": time.strftime("%H:%M"),
            "notes": ["The odds service is out of credits, so only Kalshi is shown (free public prices). Each leg is priced one by one and multiplied, which is a close estimate of the parlay price, not a quote."]}


@app.route("/")
def index():
    return send_file(PAGE if os.path.exists(PAGE) else OLD_PAGE)


@app.route("/old")
def old():
    return send_file(OLD_PAGE)


if __name__ == "__main__":
    if "--check" in sys.argv:
        try:
            s = build()["stats"]
            print(f"date: {s['date']}\ngames: {s['games']}\nteams: {s['teams']}\nplayers: {s['players']}")
            print(f"game logs (this season): {s['logs_current_season']}\ngame logs (previous season): {s['logs_previous_season']}")
            print("sources:", s["sources"])
        except Exception as e:
            print("FAILED:", type(e).__name__, e)
            sys.exit(1)
    else:
        port = int(os.environ.get("PORT", "8000"))
        app.run(host="0.0.0.0" if os.environ.get("PORT") else "127.0.0.1", port=port)
