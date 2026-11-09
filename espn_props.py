"""DraftKings player-prop prices, published free by ESPN (no key, no monthly limit).

ESPN lists each player's main DraftKings line per stat: "Total Points 17.5" with two prices, Over first
(checked against DraftKings' own numbers). A leg like "18+ points" is priced from the Over at 17.5.
ESPN's "Milestones" entries are not used: their numbers don't match the full-game lines.
Lines DraftKings doesn't list here get no price, so nothing is guessed.
"""
import datetime as dt
import re
import time
from zoneinfo import ZoneInfo

import requests

CORE = "https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba"
SITE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
TTL = 600                      # prices are reused for 10 minutes
DK = "100"                     # ESPN's id for DraftKings
TOTAL = {"pts": "Total Points", "reb": "Total Rebounds", "ast": "Total Assists", "tpm": "Total 3-Point Field Goals"}
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


def _games():
    """{frozenset({team, team}): event id} for today and tomorrow (Eastern)."""
    from espn import tri as fix
    out = {}
    today = dt.datetime.now(ZoneInfo("America/New_York")).date()
    for d in (today, today + dt.timedelta(days=1)):
        j = _cached("sb" + d.strftime("%Y%m%d"), lambda d=d: _get(SITE + "/scoreboard", {"dates": d.strftime("%Y%m%d")}))
        for ev in j.get("events") or []:
            teams = [fix(c.get("team", {}).get("abbreviation", "")) for c in (ev.get("competitions") or [{}])[0].get("competitors") or []]
            if len(teams) == 2:
                out.setdefault(frozenset(teams), str(ev["id"]))
    return out


def _props(eid):
    """{(athlete id, stat): {"total": (line, over american)}} for one game."""
    def load():
        j = _get(f"{CORE}/events/{eid}/competitions/{eid}/odds/{DK}/propBets", {"lang": "en", "region": "us", "limit": 1000})
        seen, out = set(), {}
        for it in j.get("items") or []:
            name = (it.get("type") or {}).get("name", "")
            m = re.search(r"athletes/(\d+)", (it.get("athlete") or {}).get("$ref", ""))
            if not m:
                continue
            aid = m.group(1)
            try:
                line = float((it.get("current") or {}).get("target", {}).get("value"))
                am = int(str(it["odds"]["american"]["value"]).replace("+", ""))
            except (KeyError, TypeError, ValueError):
                continue
            for s in TOTAL:
                if name == TOTAL[s] and (aid, s) not in seen:      # the first of each pair is the Over
                    seen.add((aid, s))
                    out.setdefault((aid, s), {})["total"] = (line, am)
        return out
    return _cached("pb" + eid, load)


def leg_prices(legs):
    """One dict per leg, like the other sources: {"draftkings": ("DraftKings", american)} when listed."""
    rows = [{} for _ in legs]
    try:
        games = _games()
    except Exception:
        return rows
    for i, l in enumerate(legs):
        eid = games.get(frozenset({l.get("t"), l.get("opp")}))
        pid, s = str(l.get("pid") or ""), l.get("s")
        if not eid or not pid or s not in TOTAL:
            continue
        try:
            p = _props(eid).get((pid, s)) or {}
        except Exception:
            continue
        bar = float(l.get("bar") or 0)
        if p.get("total") and p["total"][0] == bar - 0.5:
            rows[i]["draftkings"] = ("DraftKings", p["total"][1])
    return rows


def main_lines(legs):
    """DraftKings' main line for each leg's player and stat, as "N+" with the Over price, or None."""
    out = [None] * len(legs)
    try:
        games = _games()
    except Exception:
        return out
    for i, l in enumerate(legs):
        eid = games.get(frozenset({l.get("t"), l.get("opp")}))
        pid, s = str(l.get("pid") or ""), l.get("s")
        if not eid or not pid or s not in TOTAL:
            continue
        try:
            t = (_props(eid).get((pid, s)) or {}).get("total")
        except Exception:
            continue
        if t:
            out[i] = {"pid": pid, "s": s, "line": t[0] + 0.5, "american": t[1]}
    return out


def lines():
    """Which DraftKings lines exist, for debugging: number of priced (player, stat) pairs per game."""
    out = {}
    for eid in set(_games().values()):
        try:
            out[eid] = len(_props(eid))
        except Exception:
            out[eid] = 0      # not posted yet (usually tomorrow's games)
    return out
