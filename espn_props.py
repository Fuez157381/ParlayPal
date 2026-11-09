"""DraftKings player-prop prices, published free by ESPN (no key, no monthly limit). NBA and NFL.

ESPN lists each player's main DraftKings line per stat: "Total Points 17.5" with two prices, Over first
(checked against DraftKings' own numbers). A leg like "18+ points" is priced from the Over at 17.5.
Yes/no bets (double-double, anytime touchdown) have one "Yes" price, stored as a 0.5 line ("1+").
ESPN's "Milestones" entries are not used: their numbers don't match the full-game lines.
Lines DraftKings doesn't list here get no price, so nothing is guessed.
"""
import datetime as dt
import re
import time
from zoneinfo import ZoneInfo

import requests

TTL = 600                      # prices are reused for 10 minutes
DK = "100"                     # ESPN's id for DraftKings
SPORTS = {
    "nba": {"core": "https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba",
            "site": "https://site.api.espn.com/apis/site/v2/sports/basketball/nba",
            "total": {"pts": "Total Points", "reb": "Total Rebounds", "ast": "Total Assists", "tpm": "Total 3-Point Field Goals",
                      "stl": "Total Steals", "blk": "Total Blocks", "pra": "Total Points, Rebounds, and Assists",
                      "pr": "Total Points and Rebounds", "pa": "Total Points and Assists", "ra": "Total Assists and Rebounds"},
            "yes": {"dd": "To Record a Double Double", "td": "To Record a Triple Double"}},
    "nfl": {"core": "https://sports.core.api.espn.com/v2/sports/football/leagues/nfl",
            "total": {"pyd": "Total Passing Yards (incl. overtime)", "ptd": "Total Passing Touchdowns (incl. overtime)",
                      "cmp": "Total Pass Completions (incl. overtime)", "ryd": "Total Rushing Yards (incl. overtime)",
                      "car": "Total Carries (incl. overtime)", "rec": "Total Receptions (incl. overtime)",
                      "recy": "Total Receiving Yards (incl. overtime)", "rry": "Total Rushing Plus Receiving Yards (incl. overtime)"},
            "yes": {"atd": "Anytime Touchdown Scorer"}},
    "nhl": {"core": "https://sports.core.api.espn.com/v2/sports/hockey/leagues/nhl",
            "total": {"hpt": "Total Points", "hast": "Total Assists", "sog": "Total Shots on Goal", "svs": "Total Saves"},
            "yes": {"hgl": "Anytime Goalscorer"}},
    # soccer player bets are "N+" milestones (2+ shots), which match the page's "N+" legs directly
    "soc": {"core": "https://sports.core.api.espn.com/v2/sports/soccer/leagues/{lg}",
            "total": {}, "yes": {"sgl": "Anytime Goalscorer"},
            "mile": {"sht": "Shots Milestones", "sot": "Shots on Target Milestones", "sast": "Assists Milestones"}},
}
TOTAL = SPORTS["nba"]["total"]          # kept for older callers
_cache = {}


def _get(url, params=None):
    r = requests.get(url, params=params, timeout=15, headers={"User-Agent": "ParlayPal/1.0"})
    r.raise_for_status()
    return r.json()


def _cached(key, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < TTL:
        return hit[1]
    val = fn()
    _cache[key] = (time.time(), val)
    return val


def sport_of(leg):
    lg = str(leg.get("lg") or "").lower()
    return lg if lg in ("nfl", "nhl", "soc") else "nba"


def _games(sport="nba"):
    """{frozenset({team, team}): ESPN event id} for upcoming games, keyed the way the page names teams."""
    out = {}
    if sport in ("nfl", "nhl", "soc"):
        sched = _cached(sport + ":sched", __import__({"soc": "soccer"}.get(sport, sport)).fetch_schedule)
        for rows in sched.values():
            for g in rows:
                out.setdefault(frozenset({g["a"], g["h"]}), g["id"].split(":", 1)[-1])   # soccer keeps "league:id"
        return out
    from espn import tri as fix
    today = dt.datetime.now(ZoneInfo("America/New_York")).date()
    for d in (today, today + dt.timedelta(days=1)):
        j = _cached("sb" + d.strftime("%Y%m%d"), lambda d=d: _get(SPORTS["nba"]["site"] + "/scoreboard", {"dates": d.strftime("%Y%m%d")}))
        for ev in j.get("events") or []:
            teams = [fix(c.get("team", {}).get("abbreviation", "")) for c in (ev.get("competitions") or [{}])[0].get("competitors") or []]
            if len(teams) == 2:
                out.setdefault(frozenset(teams), str(ev["id"]))
    return out


def _props(eid, sport="nba"):
    """{(athlete id, stat): {"total": (line, over american)}} for one game."""
    cfg = SPORTS[sport]
    core, ev = cfg["core"], eid
    if sport == "soc":
        lg, ev = str(eid).split(":")
        core = core.format(lg=lg)

    def load():
        items = []
        for page in range(1, 6):     # big games list more than 1,000 bets
            j = _get(f"{core}/events/{ev}/competitions/{ev}/odds/{DK}/propBets", {"lang": "en", "region": "us", "limit": 1000, "page": page})
            items += j.get("items") or []
            if page >= int(j.get("pageCount") or 1):
                break
        seen, out = set(), {}
        for it in items:
            name = (it.get("type") or {}).get("name", "")
            m = re.search(r"athletes/(\d+)", (it.get("athlete") or {}).get("$ref", ""))
            if not m:
                continue
            aid = m.group(1)
            try:     # ESPN sometimes lists a line before DraftKings' price is filled in
                am = int(str(it["odds"]["american"]["value"]).replace("+", ""))
            except (KeyError, TypeError, ValueError):
                am = None
            for s, nm in cfg["yes"].items():
                if name == nm:
                    out.setdefault((aid, s), {})["total"] = (0.5, am)    # "1+" of a yes/no bet
            try:
                line = float((it.get("current") or {}).get("target", {}).get("value"))
            except (KeyError, TypeError, ValueError):
                continue
            for s, nm in cfg.get("mile", {}).items():
                if name == nm and (aid, s) not in seen:      # "2+" is stored like an Over at 1.5
                    seen.add((aid, s))
                    out.setdefault((aid, s), {})["total"] = (line - 0.5, am)
            for s, nm in cfg["total"].items():
                if name == nm and (aid, s) not in seen:      # the first of each pair is the Over
                    seen.add((aid, s))
                    out.setdefault((aid, s), {})["total"] = (line, am)
        return out
    return _cached(f"pb:{sport}:{eid}", load)


def _leg_total(l):
    sport = sport_of(l)
    eid = _games(sport).get(frozenset({l.get("t"), l.get("opp")}))
    pid, s = str(l.get("pid") or ""), l.get("s")
    if not eid or not pid or (s not in SPORTS[sport]["total"] and s not in SPORTS[sport]["yes"] and s not in SPORTS[sport].get("mile", {})):
        return None
    return (_props(eid, sport).get((pid, s)) or {}).get("total")


def leg_prices(legs):
    """One dict per leg, like the other sources: {"draftkings": ("DraftKings", american)} when listed."""
    rows = [{} for _ in legs]
    for i, l in enumerate(legs):
        try:
            t = _leg_total(l)
        except Exception:
            continue
        if t and t[1] is not None and t[0] == float(l.get("bar") or 0) - 0.5:
            rows[i]["draftkings"] = ("DraftKings", t[1])
    return rows


def main_lines(legs):
    """DraftKings' main line for each leg's player and stat, as "N+" with the Over price, or None."""
    out = [None] * len(legs)
    for i, l in enumerate(legs):
        try:
            t = _leg_total(l)
        except Exception:
            continue
        if t:
            out[i] = {"pid": str(l.get("pid")), "s": l.get("s"), "line": t[0] + 0.5, "american": t[1]}
    return out


def posted_lines(games, names, sport="nba"):
    """For /api/lines: {"norm name|stat": {N}} where N+ is DraftKings' main line, for the given games.
    names maps ESPN athlete id -> player name."""
    from kalshi import norm
    players, posted = {}, {}
    try:
        ids = _games(sport)
    except Exception:
        return players, posted
    for g in games or []:
        a, h = str(g.get("a", "")), str(g.get("h", ""))
        if sport == "nba":
            a, h = a.upper(), h.upper()
        eid = ids.get(frozenset({a, h}))
        if not eid:
            continue
        try:
            props = _props(eid, sport)
        except Exception:
            continue
        if props:
            posted[a] = posted[h] = True
        for (aid, s), v in props.items():
            nm = names.get(str(aid))
            if nm and v.get("total"):
                players.setdefault(f"{norm(nm)}|{s}", set()).add(int(round(v["total"][0] + 0.5)))
    return players, posted


def lines(sport="nba"):
    """For debugging: priced (player, stat) pairs per upcoming game."""
    out = {}
    for eid in set(_games(sport).values()):
        try:
            out[eid] = len(_props(eid, sport))
        except Exception:
            out[eid] = 0      # not posted yet
    return out
