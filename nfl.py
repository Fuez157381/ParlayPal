"""NFL data for Parlay Pal, all free from ESPN (works from cloud servers, no key).

Same shape as the NBA's /api/data so the page's research works on football too:
games, players (last games for each stat, newest first), opp (how much each defense allows vs the
league), why (the numbers behind that), net (average scoring margin, for game-script risk), dates.

NFL team keys are lowercase ("kc", "dal") so they never collide with NBA keys ("DAL"), and game ids
are "nfl:<espn id>" so live tracking and Social grading know which sport to read.
"""
import datetime as dt
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

import requests

SITE = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"
WEB = "https://site.web.api.espn.com/apis/common/v3/sports/football/nfl"
ET = ZoneInfo("America/New_York")
SKILL = {"QB", "RB", "FB", "WR", "TE"}
MAX_GAMES = 12
TTL_SCHED, TTL_LOGS, TTL_INJ = 30 * 60, 6 * 3600, 20 * 60
# stat keys the page uses, from ESPN game-log names
STATS = ("pyd", "ptd", "cmp", "ryd", "car", "rec", "recy", "rry", "atd")
BY_POS = {"QB": ("pyd", "ptd", "cmp", "ryd", "car", "rry", "atd"),
          "RB": ("ryd", "car", "rec", "recy", "rry", "atd"), "FB": ("ryd", "car", "rec", "recy", "rry", "atd"),
          "WR": ("rec", "recy", "ryd", "rry", "atd"), "TE": ("rec", "recy", "rry", "atd")}
NAMES = {"pyd": "passing yards", "ptd": "passing touchdowns", "cmp": "completions", "ryd": "rushing yards",
         "car": "carries", "rec": "receptions", "recy": "receiving yards", "rry": "rushing + receiving yards",
         "atd": "touchdowns"}


def key(abbr):
    return str(abbr or "").lower()


def _get(url, params=None, timeout=20):
    r = requests.get(url, params=params, timeout=timeout, headers={"User-Agent": "Mozilla/5.0 ParlayPal"})
    r.raise_for_status()
    return r.json()


def _num(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def season_year(today=None):
    """NFL seasons are named by the year they start (the 2026 season runs into early 2027)."""
    today = today or dt.datetime.now(ET).date()
    return today.year if today.month >= 3 else today.year - 1


# ---------- schedule ----------
def fetch_schedule():
    """{ET date: [game]} for this week and next week."""
    cur = _get(SITE + "/scoreboard")
    wk, st = (cur.get("week") or {}).get("number"), (cur.get("season") or {}).get("type", 2)
    evs = list(cur.get("events") or [])
    if wk:
        try:
            evs += _get(SITE + "/scoreboard", {"week": int(wk) + 1, "seasontype": st}).get("events") or []
        except Exception:
            pass
    out, seen = {}, set()
    for e in evs:
        if e["id"] in seen:
            continue
        seen.add(e["id"])
        comp = (e.get("competitions") or [{}])[0]
        teams = {c.get("homeAway"): key((c.get("team") or {}).get("abbreviation")) for c in comp.get("competitors") or []}
        when = dt.datetime.fromisoformat(e["date"].replace("Z", "+00:00")).astimezone(ET)
        stt = ((e.get("status") or {}).get("type") or {})
        out.setdefault(when.strftime("%Y-%m-%d"), []).append({
            "id": "nfl:" + str(e["id"]), "a": teams.get("away", ""), "h": teams.get("home", ""), "utc": e["date"],
            "status": stt.get("shortDetail") or "", "final": stt.get("state") == "post", "live": stt.get("state") == "in"})
    return out


def fetch_teams():
    j = _get(SITE + "/teams")
    out = {}
    for t in (j.get("sports") or [{}])[0].get("leagues", [{}])[0].get("teams", []):
        t = t.get("team") or {}
        out[key(t.get("abbreviation"))] = {"id": t.get("id"), "n": t.get("name") or t.get("displayName")}
    return out


def fetch_roster(team_id):
    """Skill-position players: [(athlete id, name, position)]."""
    j = _get(f"{SITE}/teams/{team_id}/roster")
    out = []
    for grp in j.get("athletes") or []:
        for a in grp.get("items") or []:
            pos = ((a.get("position") or {}).get("abbreviation") or "").upper()
            if pos in SKILL and a.get("id"):
                out.append((str(a["id"]), a.get("fullName") or a.get("displayName") or "", pos))
    return out


# ---------- game logs ----------
def fetch_gamelog(aid, year):
    j = _get(f"{WEB}/athletes/{aid}/gamelog", {"season": year})
    names = j.get("names") or []
    ix = {k: i for i, k in enumerate(names)}
    meta = j.get("events") or {}
    rows = []
    for stype in j.get("seasonTypes") or []:
        if "regular" not in str(stype.get("displayName", "")).lower():
            continue
        for cat in stype.get("categories") or []:
            for ev in cat.get("events") or []:
                s, m = ev.get("stats") or [], meta.get(str(ev.get("eventId"))) or {}
                if not m.get("gameDate"):
                    continue
                g = lambda k: _num(s[ix[k]]) if k in ix and ix[k] < len(s) else 0.0  # noqa: E731
                pyd, ryd, recy = g("passingYards"), g("rushingYards"), g("receivingYards")
                tds = g("rushingTouchdowns") + g("receivingTouchdowns")
                car, tgt, patt = g("rushingAttempts"), g("receivingTargets"), g("passingAttempts")
                a, b = (str(m.get("score") or "0-0").split("-") + ["0"])[:2]
                margin = abs(_num(a) - _num(b)) * (1 if m.get("gameResult") == "W" else -1 if m.get("gameResult") == "L" else 0)
                rows.append({
                    "gid": str(ev["eventId"]), "date": dt.datetime.fromisoformat(m["gameDate"].replace("Z", "+00:00")).astimezone(ET).strftime("%Y-%m-%d"),
                    "team": key((m.get("team") or {}).get("abbreviation")), "opp": key((m.get("opponent") or {}).get("abbreviation")),
                    "home": m.get("atVs") != "@", "margin": margin,
                    "pyd": pyd, "ptd": g("passingTouchdowns"), "cmp": g("completions"), "patt": patt,
                    "ryd": ryd, "car": car, "rec": g("receptions"), "recy": recy, "tgt": tgt,
                    "rry": ryd + recy, "atd": 1 if tds > 0 else 0, "use": patt + car if patt >= 5 else car + tgt})
    rows.sort(key=lambda r: r["date"], reverse=True)
    return rows


def fetch_injuries(norm_name, inj_level):
    j = _get(SITE + "/injuries")
    out = {}
    for team in j.get("injuries") or []:
        for it in team.get("injuries") or []:
            ath = it.get("athlete") or {}
            name = ath.get("displayName") or ath.get("fullName")
            st = str(it.get("status") or "")
            lvl = "out" if st.lower() in ("injured reserve", "ir", "physically unable to perform") else inj_level(st)
            if not name or not lvl:
                continue
            why = (it.get("shortComment") or "").strip()
            label = "Out" if lvl == "out" else "Questionable"
            out[norm_name(name)] = {"s": lvl, "t": (label + (" (" + st + ")" if st and st.lower() not in ("out", "questionable") else "") + (": " + why if why else ""))[:170], "src": "ESPN"}
    return out


# ---------- build ----------
def build(date_str, cached, norm_name, inj_level):
    today = dt.datetime.now(ET).strftime("%Y-%m-%d")
    sched, src = cached("nfl:schedule", TTL_SCHED, fetch_schedule)
    dates = sorted(d for d in sched if d >= today) or sorted(sched)
    if not date_str or date_str not in sched:     # first day with a game still to play
        date_str = next((d for d in dates if any(not g["final"] and not g["live"] for g in sched[d])), (dates or [today])[0])
    rows = sched.get(date_str, [])
    games = []
    for i, g in enumerate(rows, 1):
        when = dt.datetime.fromisoformat(g["utc"].replace("Z", "+00:00")).astimezone(ET)
        label = "Final" if g["final"] else (g["status"] if g["live"] else f"{when.hour % 12 or 12}:{when:%M} {'AM' if when.hour < 12 else 'PM'}")
        y, m, d = date_str.split("-")
        day = dt.date(int(y), int(m), int(d))
        games.append({"i": i, "id": g["id"], "a": g["a"], "h": g["h"], "d": f"{day:%a %b} {day.day}", "t": label, "lg": "nfl"})
    playing = {t for g in games for t in (g["a"], g["h"])}

    teams, _ = cached("nfl:teams", 7 * 24 * 3600, fetch_teams)
    year = season_year()

    def roster(item):
        k, t = item
        try:
            r, _ = cached(f"nfl:roster:{k}", TTL_LOGS, lambda: fetch_roster(t["id"]))
            return [(aid, n, pos, k) for aid, n, pos in r]
        except Exception:
            return []
    with ThreadPoolExecutor(8) as ex:
        everyone = [p for lst in ex.map(roster, teams.items()) for p in lst]

    def logs(p, yr, ttl):
        try:
            r, _ = cached(f"nfl:log:{yr}:{p[0]}", ttl, lambda: fetch_gamelog(p[0], yr))
            return r
        except Exception:
            return []
    with ThreadPoolExecutor(8) as ex:
        cur = list(ex.map(lambda p: logs(p, year, TTL_LOGS), everyone))
    want = [(p, c) for p, c in zip(everyone, cur) if p[3] in playing]
    with ThreadPoolExecutor(8) as ex:
        prev = list(ex.map(lambda pc: logs(pc[0], year - 1, 30 * 24 * 3600) if len(pc[1]) < MAX_GAMES else [], want))

    # defenses: what each team allows per game vs the league, from every skill player's games this season
    allowed, gm = {}, {}
    for rows_ in cur:
        for r in rows_:
            t = allowed.setdefault((r["gid"], r["opp"]), {s: 0.0 for s in ("pyd", "ptd", "cmp", "ryd", "car", "rec", "recy", "rry", "atd")})
            for s in t:
                t[s] += r[s]
            gm.setdefault(r["team"], {})[r["gid"]] = r["margin"]
    per = {}
    for (gid, team), t in allowed.items():
        per.setdefault(team, []).append(t)
    league = {s: (sum(sum(x[s] for x in v) for v in per.values()) / max(1, sum(len(v) for v in per.values()))) for s in STATS}
    opp, why = {}, {}
    for team, lst in per.items():
        if len(lst) < 3:
            continue
        opp[team], why[team] = {}, {}
        for s in STATS:
            avg = sum(x[s] for x in lst) / len(lst)
            if league[s] > 0:
                opp[team][s] = round(avg / league[s], 2)
                why[team][s] = f"{teams.get(team, {}).get('n', team.upper())} have allowed {avg:.1f} {NAMES[s]} per game over {len(lst)} games (league average {league[s]:.1f})."
    net = {t: {"m": round(sum(v.values()) / len(v), 1), "g": len(v)} for t, v in gm.items() if v}

    try:
        inj, inj_src = cached("nfl:injuries", TTL_INJ, lambda: fetch_injuries(norm_name, inj_level))
    except Exception:
        inj, inj_src = {}, "unavailable"

    players, ncur_total, nprev_total = [], 0, 0
    for (p, c), pv in zip(want, prev):
        aid, name, pos, team = p
        allg = (c + [r for r in pv if r["gid"] not in {x["gid"] for x in c}])[:MAX_GAMES]
        if not allg or sum(r["use"] for r in allg[:5]) < 3:     # no real role on offense
            continue
        ncur_total += min(len(c), MAX_GAMES)
        nprev_total += max(0, len(allg) - len(c))
        pl = {"id": int(aid), "n": name, "t": team, "pos": pos, "lg": "nfl", "ncur": min(len(c), MAX_GAMES),
              "min": [r["use"] for r in allg], "dates": [r["date"] for r in allg],
              "mu": [f"{r['team']} {'vs.' if r['home'] else '@'} {r['opp']}" for r in allg]}
        for s in BY_POS.get(pos, ()):
            pl[s] = [r[s] for r in allg]
        hit = inj.get(norm_name(name))
        if hit:
            pl["inj"] = hit
        players.append(pl)
    return {
        "sport": "nfl", "games": games, "players": players, "opp": opp, "why": why, "net": net, "dates": dates,
        "stats": {"sport": "nfl", "date": date_str, "today": today, "games": len(games), "teams": len(playing), "players": len(players),
                  "logs_current_season": ncur_total, "logs_previous_season": nprev_total,
                  "injuries": {"source": inj_src, "listed": len(inj), "hidden": sum(1 for x in players if x.get("inj", {}).get("s") == "out")},
                  "sources": {"scoreboard": src, "current_logs": "espn", "previous_logs": "espn"},
                  "updated": dt.datetime.now(ET).isoformat(timespec="seconds")},
    }


# ---------- live box score (tracked parlays, Social grading) ----------
def box(event_id):
    eid = str(event_id).split(":")[-1]
    s = _get(SITE + "/summary", {"event": eid})
    st = (((s.get("header") or {}).get("competitions") or [{}])[0].get("status") or {}).get("type") or {}
    state = st.get("state")
    out = {"status": 3 if state == "post" else 2 if state == "in" else 1,
           "text": str(st.get("shortDetail") or st.get("detail") or "").strip(), "players": {}}
    for team in (s.get("boxscore") or {}).get("players") or []:
        for grp in team.get("statistics") or []:
            kind = grp.get("name")
            keys = grp.get("keys") or []
            ix = {k: i for i, k in enumerate(keys)}
            for a in grp.get("athletes") or []:
                aid, vals = str((a.get("athlete") or {}).get("id") or ""), a.get("stats") or []
                if not aid:
                    continue
                g = lambda k: _num(vals[ix[k]]) if k in ix and ix[k] < len(vals) else 0.0  # noqa: E731
                p = out["players"].setdefault(aid, {s: 0 for s in STATS} | {"played": True, "min": ""})
                if kind == "passing":
                    ca = str(vals[ix["completions/passingAttempts"]]) if "completions/passingAttempts" in ix else "0/0"
                    p["cmp"] = _num(ca.split("/")[0])
                    p["pyd"], p["ptd"] = g("passingYards"), g("passingTouchdowns")
                elif kind == "rushing":
                    p["ryd"], p["car"] = g("rushingYards"), g("rushingAttempts")
                    p["atd"] = 1 if p["atd"] or g("rushingTouchdowns") > 0 else 0
                elif kind == "receiving":
                    p["rec"], p["recy"] = g("receptions"), g("receivingYards")
                    p["atd"] = 1 if p["atd"] or g("receivingTouchdowns") > 0 else 0
                p["rry"] = p["ryd"] + p["recy"]
    return out
