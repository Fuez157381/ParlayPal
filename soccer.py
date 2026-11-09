"""Soccer data for Parlay Pal, all free from ESPN (works from cloud servers, no key).

Leagues: Premier League, La Liga, Serie A, Bundesliga, Ligue 1, Champions League, MLS.
Same shape as the NBA's /api/data. Team keys are "s<ESPN team id>" (ESPN's soccer team ids are the
same in every competition), and game ids are "soc:<league>:<espn id>" so live tracking can read them.

Stats: sgl (to score, yes/no), sast (to assist, yes/no), sht (shots), sot (shots on target).
"""
import datetime as dt
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

import requests

SITE = "https://site.api.espn.com/apis/site/v2/sports/soccer"
STAND = "https://site.api.espn.com/apis/v2/sports/soccer"
WEB = "https://site.web.api.espn.com/apis/common/v3/sports/soccer"
ET = ZoneInfo("America/New_York")
LEAGUES = [("eng.1", "Premier League"), ("esp.1", "La Liga"), ("ita.1", "Serie A"), ("ger.1", "Bundesliga"),
           ("fra.1", "Ligue 1"), ("uefa.champions", "Champions League"), ("usa.1", "MLS")]
DOMESTIC = [l for l, _ in LEAGUES if l != "uefa.champions"]
LNAME = dict(LEAGUES)
MAX_GAMES = 12
DAYS = 14
TTL_SCHED, TTL_LOGS = 3 * 3600, 6 * 3600
STATS = ("sgl", "sast", "sht", "sot")
NAMES = {"sgl": "goals", "sast": "assists", "sht": "shots", "sot": "shots on target"}


def key(team_id):
    return "s" + str(team_id)


def _get(url, params=None, timeout=20):
    r = requests.get(url, params=params, timeout=timeout, headers={"User-Agent": "Mozilla/5.0 ParlayPal"})
    r.raise_for_status()
    return r.json()


def _num(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


# ---------- schedule ----------
def fetch_schedule():
    """{ET date: [game]} for the next two weeks, only asking ESPN for days a league has matches."""
    today = dt.datetime.now(ET).date()
    end = today + dt.timedelta(days=DAYS)
    out, seen = {}, set()
    for lg, lname in LEAGUES:
        try:
            cal = (_get(f"{SITE}/{lg}/scoreboard").get("leagues") or [{}])[0].get("calendar") or []
        except Exception:
            continue
        days = set()
        for c in cal:
            c = c if isinstance(c, str) else (c.get("startDate") or "")
            try:
                d = dt.datetime.fromisoformat(c.replace("Z", "+00:00")).date()
            except ValueError:
                continue
            if today - dt.timedelta(days=1) <= d <= end:
                days.add(d)
        for d in sorted(days):
            try:
                evs = _get(f"{SITE}/{lg}/scoreboard", {"dates": d.strftime("%Y%m%d")}).get("events") or []
            except Exception:
                continue
            for e in evs:
                if e["id"] in seen:
                    continue
                seen.add(e["id"])
                comp = (e.get("competitions") or [{}])[0]
                teams = {c.get("homeAway"): c.get("team") or {} for c in comp.get("competitors") or []}
                if "home" not in teams or "away" not in teams:
                    continue
                when = dt.datetime.fromisoformat(e["date"].replace("Z", "+00:00")).astimezone(ET)
                if when.date() > end:
                    continue
                stt = ((e.get("status") or {}).get("type") or {})
                out.setdefault(when.strftime("%Y-%m-%d"), []).append({
                    "id": f"soc:{lg}:{e['id']}", "eid": str(e["id"]), "league": lg,
                    "a": key(teams["away"].get("id")), "h": key(teams["home"].get("id")),
                    "an": teams["away"].get("shortDisplayName") or teams["away"].get("displayName"),
                    "hn": teams["home"].get("shortDisplayName") or teams["home"].get("displayName"),
                    "aab": teams["away"].get("abbreviation"), "hab": teams["home"].get("abbreviation"),
                    "utc": e["date"], "status": stt.get("shortDetail") or "",
                    "final": stt.get("state") == "post", "live": stt.get("state") == "in"})
    return out


def fetch_standings(lg):
    """{team key: {gp, gf, ga, n, ab}} for one league table (Champions League groups included)."""
    j = _get(f"{STAND}/{lg}/standings")
    out = {}
    groups = j.get("children") or [j]
    for g in groups:
        for e in ((g.get("standings") or {}).get("entries") or []):
            t, st = e.get("team") or {}, {s.get("name"): s.get("value") for s in e.get("stats") or []}
            gp = _num(st.get("gamesPlayed"))
            if not t.get("id") or gp <= 0:
                continue
            out[key(t["id"])] = {"gp": gp, "gf": _num(st.get("pointsFor")), "ga": _num(st.get("pointsAgainst")),
                                 "n": t.get("shortDisplayName") or t.get("displayName"), "ab": t.get("abbreviation"), "lg": lg}
    return out


def fetch_roster(lg, team_id):
    """Attackers and midfielders: [(athlete id, name, position, injury text or "")]."""
    j = _get(f"{SITE}/{lg}/teams/{team_id}/roster")
    out = []
    for a in j.get("athletes") or []:
        pos = ((a.get("position") or {}).get("abbreviation") or "").upper()
        if pos not in ("F", "M") or not a.get("id"):
            continue
        inj = ""
        for it in a.get("injuries") or []:
            inj = str(it.get("status") or (it.get("type") or {}).get("description") or "Injured")
        out.append((str(a["id"]), a.get("displayName") or a.get("fullName") or "", pos, inj))
    return out


def fetch_gamelog(lg, aid):
    j = _get(f"{WEB}/{lg}/athletes/{aid}/gamelog")
    names = j.get("names") or []
    ix = {k: i for i, k in enumerate(names)}
    meta = j.get("events") or {}
    rows = []
    for stype in j.get("seasonTypes") or []:
        for cat in stype.get("categories") or []:
            for ev in cat.get("events") or []:
                s, m = ev.get("stats") or [], meta.get(str(ev.get("eventId"))) or {}
                if not m.get("gameDate"):
                    continue
                g = lambda k: _num(s[ix[k]]) if k in ix and ix[k] < len(s) else 0.0  # noqa: E731
                gl, ast = g("totalGoals"), g("goalAssists")
                rows.append({"gid": str(ev["eventId"]),
                             "date": dt.datetime.fromisoformat(m["gameDate"].replace("Z", "+00:00")).astimezone(ET).strftime("%Y-%m-%d"),
                             "home": m.get("atVs") != "@", "oab": (m.get("opponent") or {}).get("abbreviation") or "",
                             "oid": (m.get("opponent") or {}).get("id"),
                             "sgl": 1 if gl > 0 else 0, "sast": 1 if ast > 0 else 0, "sht": g("totalShots"), "sot": g("shotsOnTarget")})
    rows.sort(key=lambda r: r["date"], reverse=True)
    return rows


# ---------- build ----------
def build(date_str, cached, norm_name, inj_level):
    today = dt.datetime.now(ET).strftime("%Y-%m-%d")
    sched, src = cached("soc:schedule", TTL_SCHED, fetch_schedule)
    dates = sorted(d for d in sched if d >= today) or sorted(sched)
    if not date_str or date_str not in sched:
        date_str = next((d for d in dates if any(not g["final"] and not g["live"] for g in sched[d])), (dates or [today])[0])
    rows = sorted(sched.get(date_str, []), key=lambda g: (g["utc"], g["league"]))

    tables = {}
    for lg, _ in LEAGUES:
        try:
            t, _ = cached(f"soc:table:{lg}", TTL_SCHED, lambda lg=lg: fetch_standings(lg))
            for k, v in t.items():
                if lg != "uefa.champions" or k not in tables:      # a club's domestic table wins
                    tables[k] = v
        except Exception:
            pass

    games, teams = [], {}
    for i, g in enumerate(rows, 1):
        when = dt.datetime.fromisoformat(g["utc"].replace("Z", "+00:00")).astimezone(ET)
        label = "Final" if g["final"] else (g["status"] if g["live"] else f"{when.hour % 12 or 12}:{when:%M} {'AM' if when.hour < 12 else 'PM'}")
        y, m, d = date_str.split("-")
        day = dt.date(int(y), int(m), int(d))
        games.append({"i": i, "id": g["id"], "a": g["a"], "h": g["h"], "d": f"{day:%a %b} {day.day}", "t": label,
                      "lg": "soc", "league": LNAME.get(g["league"], g["league"])})
        teams[g["a"]] = {"n": g["an"], "ab": g["aab"]}
        teams[g["h"]] = {"n": g["hn"], "ab": g["hab"]}
    playing = {(t, g["league"]) for g in rows for t in (g["a"], g["h"])}

    # each club's games are read from its domestic league (Champions League-only clubs from the Champions League)
    def club_league(t, lg):
        return tables.get(t, {}).get("lg") if tables.get(t, {}).get("lg") in DOMESTIC else lg

    def roster(item):
        t, lg = item
        clg = club_league(t, lg)
        try:
            r, _ = cached(f"soc:roster:{clg}:{t}", TTL_LOGS, lambda: fetch_roster(clg, t[1:]))
            return [(aid, n, pos, injury, t, clg) for aid, n, pos, injury in r]
        except Exception:
            return []
    with ThreadPoolExecutor(8) as ex:
        everyone = [p for lst in ex.map(roster, sorted(playing)) for p in lst]
    seen, uniq = set(), []
    for p in everyone:
        if p[0] not in seen:
            seen.add(p[0])
            uniq.append(p)

    def logs(p):
        try:
            r, _ = cached(f"soc:log:{p[5]}:{p[0]}", TTL_LOGS, lambda: fetch_gamelog(p[5], p[0]))
            return r
        except Exception:
            return []
    with ThreadPoolExecutor(8) as ex:
        cur = list(ex.map(logs, uniq))

    # defenses and team strength from the league tables
    lg_avg = {}
    for v in tables.values():
        a = lg_avg.setdefault(v["lg"], [0.0, 0.0])
        a[0] += v["ga"]
        a[1] += v["gp"]
    opp, why, net = {}, {}, {}
    for t, v in tables.items():
        avg = lg_avg[v["lg"]][0] / max(1.0, lg_avg[v["lg"]][1])
        per = v["ga"] / v["gp"]
        f = round(per / avg, 2) if avg > 0 else 1.0
        opp[t] = {s: f for s in STATS}
        txt = f"{v['n']} have conceded {per:.2f} goals per game in {LNAME.get(v['lg'], v['lg'])} ({int(v['gp'])} games; league average {avg:.2f})."
        why[t] = {s: txt for s in STATS}
        net[t] = {"m": round((v["gf"] - v["ga"]) / v["gp"], 2), "g": int(v["gp"])}

    players = []
    ncur_total = 0
    for p, c in zip(uniq, cur):
        aid, name, pos, injury, team, clg = p
        c = c[:MAX_GAMES]
        if len(c) < 3 or sum(r["sht"] for r in c[:6]) < 2:      # no real attacking role
            continue
        ncur_total += len(c)
        oppkey = {}
        for r in c:
            oppkey[r["gid"]] = key(r["oid"]) if r.get("oid") else r["oab"]
        pl = {"id": int(aid), "n": name, "t": team, "pos": pos, "lg": "soc", "ncur": len(c), "min": [],
              "dates": [r["date"] for r in c],
              "mu": [f"{team} {'vs.' if r['home'] else '@'} {oppkey[r['gid']]}" for r in c]}
        for s in STATS:
            pl[s] = [r[s] for r in c]
        gp = tables.get(team, {}).get("gp")
        if injury:
            lvl = inj_level(injury) or "out"
            pl["inj"] = {"s": lvl, "t": ("Out" if lvl == "out" else "Questionable") + ": " + injury, "src": "ESPN"}
        elif gp and gp >= 5 and len(c) < 0.6 * min(gp, MAX_GAMES):
            pl["inj"] = {"s": "q", "t": f"Rotation risk: he has played {len(c)} of the team's last {int(min(gp, MAX_GAMES))} league matches.", "src": "games"}
        players.append(pl)
    return {
        "sport": "soc", "games": games, "players": players, "opp": opp, "why": why, "net": net, "dates": dates, "teams": teams,
        "stats": {"sport": "soc", "date": date_str, "today": today, "games": len(games), "teams": len(teams), "players": len(players),
                  "logs_current_season": ncur_total, "logs_previous_season": 0,
                  "injuries": {"source": "ESPN rosters", "listed": sum(1 for p in uniq if p[3]), "hidden": sum(1 for x in players if x.get("inj", {}).get("s") == "out")},
                  "sources": {"scoreboard": src, "current_logs": "espn", "previous_logs": "none"},
                  "updated": dt.datetime.now(ET).isoformat(timespec="seconds")},
    }


# ---------- live box score (tracked parlays, Social grading) ----------
def box(gid):
    _, lg, eid = str(gid).split(":")
    s = _get(f"{SITE}/{lg}/summary", {"event": eid})
    st = (((s.get("header") or {}).get("competitions") or [{}])[0].get("status") or {}).get("type") or {}
    state = st.get("state")
    out = {"status": 3 if state == "post" else 2 if state == "in" else 1,
           "text": str(st.get("shortDetail") or st.get("detail") or "").strip(), "players": {}}
    for team in s.get("rosters") or []:
        for p in team.get("roster") or []:
            aid = str((p.get("athlete") or {}).get("id") or "")
            if not aid:
                continue
            v = {x.get("name"): _num(x.get("value")) for x in p.get("stats") or []}
            played = v.get("appearances", 0) > 0 or p.get("starter") or p.get("subbedIn")
            out["players"][aid] = {"sgl": 1 if v.get("totalGoals", 0) > 0 else 0, "sast": 1 if v.get("goalAssists", 0) > 0 else 0,
                                   "sht": v.get("totalShots", 0), "sot": v.get("shotsOnTarget", 0), "played": bool(played), "min": ""}
    return out
