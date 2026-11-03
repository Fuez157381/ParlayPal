"""NBA data from ESPN's free public API. The NBA's own sites block cloud servers (GitHub, Vercel),
ESPN does not, so the daily refresh can run in the cloud. Nothing is made up: a player with no
real games gets no line.

Shapes match what server.py used to get from nba_api, with ESPN ids for games and players
(the live tracker and the player photos use ESPN too, so the ids line up).
"""
import datetime as dt
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

SITE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
WEB = "https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
ET = ZoneInfo("America/New_York")
# ESPN's short team codes -> the NBA codes the site uses everywhere (logos, Kalshi, odds)
ABBR = {"GS": "GSW", "NY": "NYK", "NO": "NOP", "SA": "SAS", "UTAH": "UTA", "WSH": "WAS"}
TEAMS = {"ATL", "BOS", "BKN", "CHA", "CHI", "CLE", "DAL", "DEN", "DET", "GSW", "HOU", "IND", "LAC", "LAL", "MEM",
         "MIA", "MIL", "MIN", "NOP", "NYK", "OKC", "ORL", "PHI", "PHX", "POR", "SAC", "SAS", "TOR", "UTA", "WAS"}


def tri(a):
    a = str(a or "").upper()
    return ABBR.get(a, a)


def _get(url, params=None, timeout=25):
    import requests
    last = None
    for _ in range(3):  # ESPN sometimes drops one request; try again before giving up
        try:
            r = requests.get(url, params=params, timeout=timeout, headers=HEADERS)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            last = e
    raise last


def _et_date(iso):
    when = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return when.astimezone(ET).strftime("%Y-%m-%d")


def fetch_schedule(start_year):
    """Every game of the season starting in start_year: {YYYY-MM-DD (Eastern): [games]}."""
    months = [(start_year, m) for m in (9, 10, 11, 12)] + [(start_year + 1, m) for m in range(1, 7)]
    out = {}
    for y, m in months:
        for e in _get(SITE + "/scoreboard", {"dates": f"{y}{m:02d}", "limit": 1000}).get("events", []):
            c = (e.get("competitions") or [{}])[0]
            side = {x.get("homeAway"): tri((x.get("team") or {}).get("abbreviation")) for x in c.get("competitors", [])}
            if not side.get("home") or not side.get("away"):
                continue
            st = (e.get("status") or {}).get("type") or {}
            status = "Final" if st.get("state") == "post" else (st.get("shortDetail") or "")
            out.setdefault(_et_date(e["date"]), []).append(
                {"id": str(e["id"]), "a": side["away"], "h": side["home"], "utc": e["date"], "status": status})
    if not out:
        raise ValueError("ESPN schedule came back empty")
    for rows in out.values():
        rows.sort(key=lambda g: g["utc"])
    return out


def fetch_rosters():
    """Everyone on a current NBA roster: {athlete id: {"n": name, "t": team}}."""
    teams = _get(SITE + "/teams")["sports"][0]["leagues"][0]["teams"]

    def one(t):
        t = t["team"]
        ro = _get(f"{SITE}/teams/{t['id']}/roster")
        return [(str(a["id"]), {"n": a.get("displayName") or a.get("fullName"), "t": tri(t.get("abbreviation"))})
                for a in ro.get("athletes", []) if a.get("id")]

    out = {}
    with ThreadPoolExecutor(8) as ex:
        for rows in ex.map(one, teams):
            out.update(rows)
    if len(out) < 300:
        raise ValueError(f"ESPN rosters look incomplete ({len(out)} players)")
    return out


def _num(x):
    try:
        return int(float(str(x).split("-")[0]))
    except (TypeError, ValueError):
        return 0


def fetch_gamelog(aid, season_year):
    """One player's regular-season games for an ESPN season year (2027 = the 2026-27 season).
    Rows look like server.parse_logs output, minus pid/name, which the caller adds."""
    j = _get(f"{WEB}/athletes/{aid}/gamelog", {"season": season_year})
    labels = j.get("labels") or []
    ix = {k: i for i, k in enumerate(labels)}
    if "MIN" not in ix or "PTS" not in ix:
        return []
    meta = j.get("events") or {}
    rows = []
    for st in j.get("seasonTypes") or []:
        kind = str(st.get("displayName", "")).lower()
        if "regular season" not in kind or "play in" in kind or "play-in" in kind:   # the NBA's logs skip play-in games
            continue
        for cat in st.get("categories") or []:
            for ev in cat.get("events") or []:
                s, m = ev.get("stats") or [], meta.get(str(ev.get("eventId"))) or {}
                if len(s) < len(labels) or not m.get("gameDate"):
                    continue
                mins = _num(s[ix["MIN"]])
                if mins <= 0:            # did not play: never treated as a zero-stat game
                    continue
                three = str(s[ix["3PT"]]) if "3PT" in ix else "0-0"
                team, opp = tri((m.get("team") or {}).get("abbreviation")), tri((m.get("opponent") or {}).get("abbreviation"))
                if team not in TEAMS or opp not in TEAMS or "all-star" in str(m.get("eventNote", "")).lower():
                    continue             # All-Star and exhibition games are not real team games
                rows.append({
                    "team": team, "gid": str(ev["eventId"]), "date": _et_date(m["gameDate"]), "min": mins,
                    "pts": _num(s[ix["PTS"]]), "reb": _num(s[ix["REB"]]), "ast": _num(s[ix["AST"]]),
                    "tpm": _num(three), "tpa": _num(three.split("-")[1]) if "-" in three else 0,
                    "mu": f"{team} {'@' if m.get('atVs') == '@' else 'vs.'} {opp}",
                })
    return rows


def all_logs(roster, season_year, cached, ttl):
    """Game rows for every rostered player, fetched 8 at a time and cached per player."""
    def one(item):
        aid, info = item
        try:
            rows, _ = cached(f"espn:log:{season_year}:{aid}", ttl, lambda: fetch_gamelog(aid, season_year))
        except Exception:  # noqa: BLE001  one player failing never sinks the whole day
            return []
        for r in rows:
            r["pid"], r["name"] = int(aid), info["n"]
        return rows

    out = []
    with ThreadPoolExecutor(8) as ex:
        for rows in ex.map(one, roster.items()):
            out.extend(rows)
    return out


def box(event_id):
    """Live or final box score for the parlay tracker, in live.py's shape."""
    s = _get(SITE + "/summary", {"event": event_id}, timeout=20)
    st = (((s.get("header") or {}).get("competitions") or [{}])[0].get("status") or {}).get("type") or {}
    state = st.get("state")
    out = {"status": 3 if state == "post" else 2 if state == "in" else 1,
           "text": str(st.get("shortDetail") or st.get("detail") or "").strip(), "players": {}}
    for team in (s.get("boxscore") or {}).get("players") or []:
        for grp in team.get("statistics") or []:
            ix = {k: i for i, k in enumerate(grp.get("labels") or [])}
            for a in grp.get("athletes") or []:
                aid, vals = str((a.get("athlete") or {}).get("id") or ""), a.get("stats") or []
                if not aid:
                    continue
                if a.get("didNotPlay") or not vals:
                    out["players"][aid] = {"pts": 0, "reb": 0, "ast": 0, "tpm": 0, "min": "", "played": False}
                    continue
                g = lambda k: vals[ix[k]] if k in ix and ix[k] < len(vals) else 0  # noqa: E731
                out["players"][aid] = {"pts": _num(g("PTS")), "reb": _num(g("REB")), "ast": _num(g("AST")),
                                       "tpm": _num(g("3PT")), "min": str(g("MIN")), "played": True}
    return out
