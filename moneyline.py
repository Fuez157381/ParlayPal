"""Moneylines (team to win) for Parlay Pal: NBA, NFL, NHL and soccer.

Adds one entry per team in each game to a sport's /api/data, shaped like a player so the page's research,
parlay and tracking work unchanged. Stat "ml" = the team wins (soccer: wins in regular time, a draw loses).
Research, all real results from ESPN team schedules: last 10 results and margins, home and away records,
streak, rest days, who is out, and the opponent's form. The price is Kalshi's game market (free, public).
"""
import datetime as dt
import re
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

import requests

ET = ZoneInfo("America/New_York")
SITE = "https://site.api.espn.com/apis/site/v2/sports"
KAL = "https://api.elections.kalshi.com/trade-api/v2"
PATH = {"nba": "basketball/nba", "nfl": "football/nfl", "nhl": "hockey/nhl", "cfb": "football/college-football"}
KSER = {"cfb": ("KXNCAAFGAME",), "nba": ("KXNBAGAME",), "nfl": ("KXNFLGAME",), "nhl": ("KXNHLGAME",),
        "soc": ("KXEPLGAME", "KXLALIGAGAME", "KXSERIEAGAME", "KXBUNDESLIGAGAME", "KXLIGUE1GAME", "KXUCLGAME", "KXMLSGAME")}
SOC_LG = {"Premier League": "eng.1", "La Liga": "esp.1", "Serie A": "ita.1", "Bundesliga": "ger.1", "Ligue 1": "fra.1",
          "Champions League": "uefa.champions", "MLS": "usa.1"}
ALIAS = {"GSW": ("GSW", "GS"), "NYK": ("NYK", "NY"), "NOP": ("NOP", "NO"), "SAS": ("SAS", "SA"), "UTA": ("UTA", "UTAH"),
         "WAS": ("WAS", "WSH"), "WSH": ("WAS", "WSH"), "JAX": ("JAX", "JAC"), "LAR": ("LA", "LAR"),
         "UTAH": ("UTA", "UTAH"), "LA": ("LA", "LAK"), "TB": ("TB", "TBL"), "NJ": ("NJ", "NJD"), "SJ": ("SJ", "SJS")}
TTL = 3 * 3600
LAST = 10


def _get(url, params=None):
    r = requests.get(url, params=params, timeout=20, headers={"User-Agent": "Mozilla/5.0 ParlayPal"})
    r.raise_for_status()
    return r.json()


def key_of(sport, abbr, tid=None):
    if sport == "nba":
        from espn import tri
        return tri(abbr)
    if sport == "nfl":
        return str(abbr or "").lower()
    if sport == "nhl":
        a = str(abbr or "")
        return a[:1].upper() + a[1:].lower()
    if sport == "cfb":
        return "cf" + str(tid)
    return "s" + str(tid)


def team_ids(sport):
    """{page team key: (espn id, name)} for one league."""
    j = _get(f"{SITE}/{PATH[sport]}/teams")
    out = {}
    for t in (j.get("sports") or [{}])[0].get("leagues", [{}])[0].get("teams", []):
        t = t.get("team") or {}
        out[key_of(sport, t.get("abbreviation"))] = (t.get("id"), t.get("shortDisplayName") or t.get("name"), t.get("abbreviation"))
    return out


def schedule(sport, tid, lg=None):
    """Completed games, newest first: {date, opp, home, win, us, them}."""
    url = f"{SITE}/soccer/{lg}/teams/{tid}/schedule" if sport == "soc" else f"{SITE}/{PATH[sport]}/teams/{tid}/schedule"
    j = _get(url)
    rows = []
    for e in j.get("events") or []:
        c = (e.get("competitions") or [{}])[0]
        if not ((c.get("status") or {}).get("type") or {}).get("completed"):
            continue
        cs = c.get("competitors") or []
        me = next((x for x in cs if str((x.get("team") or {}).get("id")) == str(tid)), None)
        op = next((x for x in cs if str((x.get("team") or {}).get("id")) != str(tid)), None)
        if not me or not op:
            continue
        def sc(x):
            v = x.get("score")
            v = v.get("value") if isinstance(v, dict) else v
            try:
                return float(v)
            except (TypeError, ValueError):
                return 0.0
        us, them = sc(me), sc(op)
        ot = op.get("team") or {}
        rows.append({"date": dt.datetime.fromisoformat(c.get("date", e.get("date")).replace("Z", "+00:00")).astimezone(ET).strftime("%Y-%m-%d"),
                     "opp": key_of(sport, ot.get("abbreviation"), ot.get("id")), "oppName": ot.get("shortDisplayName") or ot.get("displayName"),
                     "home": me.get("homeAway") == "home", "win": 1 if us > them else 0, "draw": us == them, "us": us, "them": them})
    rows.sort(key=lambda r: r["date"], reverse=True)
    return rows


def kalshi_games(sport):
    """{(code, code): {code: (cents, ticker)}} for Kalshi's open game markets."""
    out = {}
    for ser in KSER[sport]:
        try:
            ms = _get(KAL + "/markets", {"series_ticker": ser, "status": "open", "limit": 1000}).get("markets") or []
        except Exception:
            continue
        for m in ms:
            tk = m.get("ticker") or ""
            side = tk.rsplit("-", 1)[-1]
            ev = (m.get("event_ticker") or "").split("-", 1)[-1]
            c = None
            for k in ("yes_ask_dollars", "yes_ask"):
                try:
                    v = float(m.get(k))
                    c = round(v * 100) if (k.endswith("dollars") or v <= 1) else round(v)
                    break
                except (TypeError, ValueError):
                    pass
            out.setdefault(ev, {})[side] = (c, tk)
    return out


def _codes(k, ab=None):
    base = str(ab or k).upper()
    return ALIAS.get(base, (base,))


def _price(kg, a, b, ab_a, ab_b):
    """Find a's Kalshi win market in the game a vs b."""
    for ev, sides in kg.items():
        body = re.sub(r"^\d{2}[A-Z]{3}\d{2}(\d{4})?", "", ev)
        for ca in _codes(a, ab_a):
            for cb in _codes(b, ab_b):
                if body in (ca + cb, cb + ca) and ca in sides:
                    return sides[ca]
    return (None, None)


def build_cfb(date_str, cached):
    """College football: this week's featured games (ranked teams first) as moneylines."""
    today = dt.datetime.now(ET).strftime("%Y-%m-%d")
    def fetch():
        j = _get(f"{SITE}/{PATH['cfb']}/scoreboard")
        out = []
        for e in j.get("events") or []:
            c = (e.get("competitions") or [{}])[0]
            st = ((c.get("status") or {}).get("type") or {})
            if st.get("state") == "post":
                continue
            sides = {}
            for x in c.get("competitors") or []:
                t = x.get("team") or {}
                rk = (x.get("curatedRank") or {}).get("current")
                sides[x.get("homeAway")] = {"key": "cf" + str(t.get("id")), "n": t.get("shortDisplayName") or t.get("displayName"),
                                            "ab": t.get("abbreviation"), "rank": rk if rk and rk <= 25 else None}
            if "home" in sides and "away" in sides:
                out.append({"id": "cfb:" + str(e["id"]), "utc": e["date"], "a": sides["away"], "h": sides["home"],
                            "status": st.get("shortDetail") or "", "live": st.get("state") == "in"})
        return out
    evs, src = cached("cfb:week", 30 * 60, fetch)
    bydate = {}
    for g in evs:
        d = dt.datetime.fromisoformat(g["utc"].replace("Z", "+00:00")).astimezone(ET)
        bydate.setdefault(d.strftime("%Y-%m-%d"), []).append((d, g))
    dates = sorted(bydate)
    if not date_str or date_str not in bydate:
        date_str = (dates or [today])[0]
    games, teams = [], {}
    for i, (d, g) in enumerate(sorted(bydate.get(date_str, []), key=lambda x: (x[0], -(bool(x[1]["a"]["rank"]) + bool(x[1]["h"]["rank"])))), 1):
        games.append({"i": i, "id": g["id"], "a": g["a"]["key"], "h": g["h"]["key"], "d": f"{d:%a %b} {d.day}",
                      "t": g["status"] if g["live"] else f"{d.hour % 12 or 12}:{d:%M} {'AM' if d.hour < 12 else 'PM'}", "lg": "cfb"})
        for sd in (g["a"], g["h"]):
            teams[sd["key"]] = {"n": ("#" + str(sd["rank"]) + " " if sd["rank"] else "") + sd["n"], "ab": sd["ab"]}
    data = {"sport": "cfb", "games": games, "players": [], "opp": {}, "why": {}, "net": {}, "dates": dates, "teams": teams,
            "stats": {"sport": "cfb", "date": date_str, "today": today, "games": len(games), "teams": len(teams), "players": 0,
                      "logs_current_season": 0, "logs_previous_season": 0, "injuries": {"source": "none", "listed": 0, "hidden": 0},
                      "sources": {"scoreboard": src}, "updated": dt.datetime.now(ET).isoformat(timespec="seconds")}}
    add(data, "cfb", cached)
    data["stats"]["players"] = len(data["players"])
    return data


def box_teams(gid):
    """Final result per team for moneyline legs: {"ml<team key>": {"ml": 1 or 0}}."""
    g = str(gid)
    sport, eid, lg = "nba", g, None
    if g.startswith("soc:"):
        sport, lg, eid = "soc", g.split(":")[1], g.split(":")[2]
    elif ":" in g:
        sport, eid = g.split(":", 1)
    if sport not in PATH and sport != "soc":
        return {}
    url = f"{SITE}/soccer/{lg}/summary" if sport == "soc" else f"{SITE}/{PATH[sport]}/summary"
    s = _get(url, {"event": eid})
    comp = ((s.get("header") or {}).get("competitions") or [{}])[0]
    if ((comp.get("status") or {}).get("type") or {}).get("state") != "post":
        return {}
    out = {}
    cs = comp.get("competitors") or []
    def sc(x):
        try:
            return float(x.get("score"))
        except (TypeError, ValueError):
            return 0.0
    for x in cs:
        t = x.get("team") or {}
        others = [sc(o) for o in cs if o is not x]
        won = bool(x.get("winner")) if x.get("winner") is not None else (others and sc(x) > max(others))
        out["ml" + key_of(sport, t.get("abbreviation"), t.get("id"))] = {"ml": 1 if won else 0, "played": True, "min": ""}
    return out


def add(data, sport, cached):
    """Adds moneyline entries to a sport's day data (in place) and returns it."""
    games = data.get("games") or []
    if not games or sport not in KSER:
        return data
    try:
        kg, _ = cached(f"ml:kalshi:{sport}", 20 * 60, lambda: kalshi_games(sport))
    except Exception:
        kg = {}
    if sport in ("soc", "cfb"):
        ids = {k: (k[2:] if sport == "cfb" else k[1:], (data.get("teams") or {}).get(k, {}).get("n"), (data.get("teams") or {}).get(k, {}).get("ab")) for g in games for k in (g["a"], g["h"])}
    else:
        try:
            ids, _ = cached(f"ml:teams:{sport}", 7 * 24 * 3600, lambda: team_ids(sport))
        except Exception:
            return data
    lgof = {}
    for g in games:
        for k in (g["a"], g["h"]):
            lgof[k] = SOC_LG.get(g.get("league"), "eng.1")

    def load(k):
        tid = (ids.get(k) or (None,))[0]
        if not tid:
            return k, []
        try:
            r, _ = cached(f"ml:sched:{sport}:{tid}:{lgof.get(k, '')}", TTL, lambda: schedule(sport, tid, lgof.get(k)))
            return k, r
        except Exception:
            return k, []
    with ThreadPoolExecutor(8) as ex:
        sched = dict(ex.map(load, sorted({k for g in games for k in (g["a"], g["h"])})))
    out_by = {}
    for p in data.get("players") or []:
        if (p.get("inj") or {}).get("s") == "out":
            out_by.setdefault(p.get("t"), []).append(p)
    day = (data.get("stats") or {}).get("date")
    entries = []
    for g in games:
        for me, op, home in ((g["a"], g["h"], False), (g["h"], g["a"], True)):
            if day:                                        # only games before this one count
                sched[me] = [r for r in sched.get(me, []) if r["date"] < day]
                sched[op] = [r for r in sched.get(op, []) if r["date"] < day]
            rows = sched.get(me, [])[:LAST]
            if len(rows) < 3:
                continue
            name = (ids.get(me) or (None, me))[1] or me
            oname = (ids.get(op) or (None, op))[1] or op
            cents, tk = _price(kg, me, op, (ids.get(me) or (None, None, None))[2], (ids.get(op) or (None, None, None))[2])
            wins = sum(r["win"] for r in rows)
            mar = [r["us"] - r["them"] for r in rows]
            allr = sched.get(me, [])
            hr = [r for r in allr if r["home"] == home][:LAST]
            info = [f"Won {wins} of the last {len(rows)}, average margin {sum(mar) / len(mar):+.1f}."]
            if hr:
                info.append(f"{'Home' if home else 'Away'} record this season: {sum(r['win'] for r in hr)}-{len(hr) - sum(r['win'] for r in hr)}" +
                            (f" ({sum(1 for r in hr if r['draw'])} draws)." if sport == "soc" and any(r['draw'] for r in hr) else "."))
            st = 0
            for r in rows:
                if r["win"] == rows[0]["win"]:
                    st += 1
                else:
                    break
            if st >= 3:
                info.append(f"On a {st}-game {'winning' if rows[0]['win'] else 'losing'} streak.")
            h2h = [r for r in allr if r["opp"] == op][:5]
            if h2h:
                info.append(f"Against the {oname} recently: {sum(r['win'] for r in h2h)}-{len(h2h) - sum(r['win'] for r in h2h)}.")
            risks = []
            if day and rows:
                rest = (dt.date.fromisoformat(day) - dt.date.fromisoformat(rows[0]["date"])).days
                if sport in ("nba", "nhl") and rest <= 1:
                    risks.append("Played yesterday: second night of a back-to-back.")
                elif rest >= 3 and sport in ("nba", "nhl"):
                    info.append(f"Rested: {rest} days since the last game.")
            outs = sorted(out_by.get(me, []), key=lambda p: -sum((p.get("min") or [0])[:5]))[:3]
            if outs:
                risks.append("Out: " + ", ".join(p["n"] for p in outs) + ".")
            orow = sched.get(op, [])[:LAST]
            if orow:
                ow = sum(r["win"] for r in orow)
                (info if ow <= len(orow) * 0.4 else risks if ow >= len(orow) * 0.7 else info).append(
                    f"The {oname} won {ow} of their last {len(orow)}.")
            if sport == "soc":
                d = sum(1 for r in rows if r["draw"])
                if d >= 3:
                    risks.append(f"{d} draws in the last {len(rows)}: a draw loses this bet.")
            entries.append({"id": "ml" + me, "n": name, "t": me, "lg": sport, "pos": "TEAM", "ncur": len(rows),
                            "dates": [r["date"] for r in rows], "min": [],
                            "mu": [f"{me} {'vs.' if r['home'] else '@'} {r['opp']}" for r in rows],
                            "ml": [r["win"] for r in rows], "mlm": mar, "kp": cents, "ktk": tk, "info": info, "risks": risks,
                            "ab": (ids.get(me) or (None, None, None))[2]})
    data["players"] = entries + (data.get("players") or [])
    return data
