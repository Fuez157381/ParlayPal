"""NHL data for Parlay Pal, all free from ESPN (works from cloud servers, no key).

Same shape as the NBA's /api/data so the page's research works on hockey too. NHL team keys are
capitalized ("Bos", "Nyr", "Utah") so they never collide with NBA ("BOS") or NFL ("ne") keys,
and game ids are "nhl:<espn id>" so live tracking and Social grading know which sport to read.

Stats: hpt (points), hast (assists), sog (shots on goal), hgl (anytime goal, yes/no), svs (goalie saves).
"""
import datetime as dt
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

import requests

SITE = "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl"
WEB = "https://site.web.api.espn.com/apis/common/v3/sports/hockey/nhl"
ET = ZoneInfo("America/New_York")
MAX_GAMES = 15
TTL_SCHED, TTL_LOGS, TTL_INJ = 30 * 60, 6 * 3600, 20 * 60
SKATER = ("hpt", "hast", "sog", "hgl")
GOALIE = ("svs",)
STATS = SKATER + GOALIE
NAMES = {"hpt": "points", "hast": "assists", "sog": "shots on goal", "hgl": "goals", "svs": "saves"}


def key(abbr):
    a = str(abbr or "")
    return a[:1].upper() + a[1:].lower()


def _get(url, params=None, timeout=20):
    r = requests.get(url, params=params, timeout=timeout, headers={"User-Agent": "Mozilla/5.0 ParlayPal"})
    r.raise_for_status()
    return r.json()


def _num(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def _mins(v):
    try:
        m, s = str(v).split(":")
        return round(int(m) + int(s) / 60, 1)
    except (ValueError, TypeError):
        return _num(v)


def season_year(today=None):
    """ESPN names an NHL season by the year it ends (2026-27 is 2027)."""
    today = today or dt.datetime.now(ET).date()
    return today.year + 1 if today.month >= 8 else today.year


# ---------- schedule ----------
def fetch_schedule(days=8):
    """{ET date: [game]} for today and the next week."""
    today = dt.datetime.now(ET).date()
    out, seen = {}, set()
    for i in range(days):
        d = today + dt.timedelta(days=i)
        try:
            evs = _get(SITE + "/scoreboard", {"dates": d.strftime("%Y%m%d")}).get("events") or []
        except Exception:
            continue
        for e in evs:
            if e["id"] in seen:
                continue
            seen.add(e["id"])
            comp = (e.get("competitions") or [{}])[0]
            teams = {c.get("homeAway"): key((c.get("team") or {}).get("abbreviation")) for c in comp.get("competitors") or []}
            when = dt.datetime.fromisoformat(e["date"].replace("Z", "+00:00")).astimezone(ET)
            stt = ((e.get("status") or {}).get("type") or {})
            out.setdefault(when.strftime("%Y-%m-%d"), []).append({
                "id": "nhl:" + str(e["id"]), "a": teams.get("away", ""), "h": teams.get("home", ""), "utc": e["date"],
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
    j = _get(f"{SITE}/teams/{team_id}/roster")
    out = []
    for grp in j.get("athletes") or []:
        for a in grp.get("items") or []:
            pos = ((a.get("position") or {}).get("abbreviation") or "").upper()
            if a.get("id") and pos:
                out.append((str(a["id"]), a.get("fullName") or a.get("displayName") or "", pos))
    return out


# ---------- game logs ----------
def fetch_gamelog(aid, year):
    j = _get(f"{WEB}/athletes/{aid}/gamelog", {"season": year})
    names = j.get("names") or []
    ix = {k: i for i, k in enumerate(names)}
    meta = j.get("events") or {}
    goalie = "saves" in ix
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
                a, b = (str(m.get("score") or "0-0").split("-") + ["0"])[:2]
                margin = abs(_num(a) - _num(b)) * (1 if m.get("gameResult") == "W" else -1 if m.get("gameResult") == "L" else 0)
                toi = _mins(s[ix["timeOnIcePerGame"]]) if "timeOnIcePerGame" in ix and ix["timeOnIcePerGame"] < len(s) else 0.0
                row = {"gid": str(ev["eventId"]),
                       "date": dt.datetime.fromisoformat(m["gameDate"].replace("Z", "+00:00")).astimezone(ET).strftime("%Y-%m-%d"),
                       "team": key((m.get("team") or {}).get("abbreviation")), "opp": key((m.get("opponent") or {}).get("abbreviation")),
                       "home": m.get("atVs") != "@", "margin": margin, "toi": toi, "goalie": goalie}
                if goalie:
                    if g("gameStarted") < 1 and toi < 30:      # relief appearances aren't real starts
                        continue
                    row.update(svs=g("saves"), sa=g("shotsAgainst"))
                else:
                    gl = g("goals")
                    row.update(hpt=g("points"), hast=g("assists"), sog=g("shotsTotal"), hgl=1 if gl > 0 else 0, goals=gl)
                rows.append(row)
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
            lvl = "out" if st.lower() in ("injured reserve", "ir", "long-term injured reserve", "ltir") else inj_level(st)
            if not name or not lvl:
                continue
            why = (it.get("shortComment") or "").strip()
            label = "Out" if lvl == "out" else "Questionable"
            out[norm_name(name)] = {"s": lvl, "t": (label + (" (" + st + ")" if st and st.lower() not in ("out", "questionable") else "") + (": " + why if why else ""))[:170], "src": "ESPN"}
    return out


# ---------- build ----------
def build(date_str, cached, norm_name, inj_level):
    today = dt.datetime.now(ET).strftime("%Y-%m-%d")
    sched, src = cached("nhl:schedule", TTL_SCHED, fetch_schedule)
    dates = sorted(d for d in sched if d >= today) or sorted(sched)
    if not date_str or date_str not in sched:     # first day with a game still to play
        date_str = next((d for d in dates if any(not g["final"] and not g["live"] for g in sched[d])), (dates or [today])[0])
    games = []
    for i, g in enumerate(sched.get(date_str, []), 1):
        when = dt.datetime.fromisoformat(g["utc"].replace("Z", "+00:00")).astimezone(ET)
        label = "Final" if g["final"] else (g["status"] if g["live"] else f"{when.hour % 12 or 12}:{when:%M} {'AM' if when.hour < 12 else 'PM'}")
        y, m, d = date_str.split("-")
        day = dt.date(int(y), int(m), int(d))
        games.append({"i": i, "id": g["id"], "a": g["a"], "h": g["h"], "d": f"{day:%a %b} {day.day}", "t": label, "lg": "nhl"})
    playing = {t for g in games for t in (g["a"], g["h"])}

    teams, _ = cached("nhl:teams", 7 * 24 * 3600, fetch_teams)
    year = season_year()

    def roster(item):
        k, t = item
        try:
            r, _ = cached(f"nhl:roster:{k}", TTL_LOGS, lambda: fetch_roster(t["id"]))
            return [(aid, n, pos, k) for aid, n, pos in r]
        except Exception:
            return []
    with ThreadPoolExecutor(8) as ex:
        everyone = [p for lst in ex.map(roster, teams.items()) for p in lst]

    def logs(p, yr, ttl):
        try:
            r, _ = cached(f"nhl:log:{yr}:{p[0]}", ttl, lambda: fetch_gamelog(p[0], yr))
            return r
        except Exception:
            return []
    with ThreadPoolExecutor(8) as ex:
        cur = list(ex.map(lambda p: logs(p, year, TTL_LOGS), everyone))
    want = [(p, c) for p, c in zip(everyone, cur) if p[3] in playing]
    with ThreadPoolExecutor(8) as ex:
        prev = list(ex.map(lambda pc: logs(pc[0], year - 1, 30 * 24 * 3600) if len(pc[1]) < MAX_GAMES else [], want))

    # what each team allows to skaters, and how many shots each team takes (a goalie's saves follow the shooters)
    allowed, shots_for, gm = {}, {}, {}
    for rows_ in cur:
        for r in rows_:
            if r["goalie"]:
                continue
            t = allowed.setdefault((r["gid"], r["opp"]), {s: 0.0 for s in SKATER})
            for s in SKATER:
                t[s] += r[s]
            shots_for[(r["gid"], r["team"])] = shots_for.get((r["gid"], r["team"]), 0) + r["sog"]
            gm.setdefault(r["team"], {})[r["gid"]] = r["margin"]
    per = {}
    for (gid, team), t in allowed.items():
        per.setdefault(team, []).append(t)
    sf = {}
    for (gid, team), v in shots_for.items():
        sf.setdefault(team, []).append(v)
    league = {s: sum(sum(x[s] for x in v) for v in per.values()) / max(1, sum(len(v) for v in per.values())) for s in SKATER}
    league_sf = sum(sum(v) for v in sf.values()) / max(1, sum(len(v) for v in sf.values()))
    opp, why = {}, {}
    for team in set(per) | set(sf):
        tn = teams.get(team, {}).get("n", team)
        opp[team], why[team] = {}, {}
        lst = per.get(team, [])
        if len(lst) >= 3:
            for s in SKATER:
                avg = sum(x[s] for x in lst) / len(lst)
                if league[s] > 0:
                    opp[team][s] = round(avg / league[s], 2)
                    why[team][s] = f"The {tn} have allowed {avg:.1f} {NAMES[s]} per game to skaters over {len(lst)} games (league average {league[s]:.1f})."
        v = sf.get(team, [])
        if len(v) >= 3 and league_sf > 0:
            a = sum(v) / len(v)
            opp[team]["svs"] = round(a / league_sf, 2)
            why[team]["svs"] = f"The {tn} take {a:.1f} shots on goal per game over {len(v)} games (league average {league_sf:.1f}), so their opponent's goalie faces {'more' if a > league_sf else 'fewer'} shots."
    net = {t: {"m": round(sum(v.values()) / len(v), 1), "g": len(v)} for t, v in gm.items() if v}
    # back-to-backs: a team that also played the day before (starting goalies usually rest)
    y, m_, d_ = (int(x) for x in date_str.split("-"))
    yday = (dt.date(y, m_, d_) - dt.timedelta(days=1)).strftime("%Y-%m-%d")
    played_yday = {r["team"] for rows_ in cur for r in rows_ if r["date"] == yday}
    played_yday |= {t for g in sched.get(yday, []) for t in (g["a"], g["h"])}
    for g in games:
        g["b2b"] = [t for t in (g["a"], g["h"]) if t in played_yday]

    try:
        inj, inj_src = cached("nhl:injuries", TTL_INJ, lambda: fetch_injuries(norm_name, inj_level))
    except Exception:
        inj, inj_src = {}, "unavailable"

    players, ncur_total, nprev_total = [], 0, 0
    for (p, c), pv in zip(want, prev):
        aid, name, pos, team = p
        goalie = pos == "G"
        allg = (c + [r for r in pv if r["gid"] not in {x["gid"] for x in c}])[:MAX_GAMES]
        allg = [r for r in allg if r["goalie"] == goalie]
        if len(allg) < 3:
            continue
        ncur_total += min(len(c), MAX_GAMES)
        nprev_total += max(0, len(allg) - len(c))
        pl = {"id": int(aid), "n": name, "t": team, "pos": pos, "lg": "nhl", "ncur": min(len(c), MAX_GAMES),
              "min": [r["toi"] for r in allg], "dates": [r["date"] for r in allg],
              "mu": [f"{r['team']} {'vs.' if r['home'] else '@'} {r['opp']}" for r in allg]}
        for s in (GOALIE if goalie else SKATER):
            pl[s] = [r[s] for r in allg]
        hit = inj.get(norm_name(name))
        if hit:
            pl["inj"] = hit
        players.append(pl)
    return {
        "sport": "nhl", "games": games, "players": players, "opp": opp, "why": why, "net": net, "dates": dates,
        "stats": {"sport": "nhl", "date": date_str, "today": today, "games": len(games), "teams": len(playing), "players": len(players),
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
            keys = grp.get("keys") or []
            ix = {k: i for i, k in enumerate(keys)}
            for a in grp.get("athletes") or []:
                aid, vals = str((a.get("athlete") or {}).get("id") or ""), a.get("stats") or []
                if not aid or not vals:
                    continue
                g = lambda k: _num(vals[ix[k]]) if k in ix and ix[k] < len(vals) else 0.0  # noqa: E731
                if grp.get("name") == "goalies":
                    out["players"][aid] = {"svs": g("saves"), "played": True, "min": str(vals[ix["timeOnIce"]]) if "timeOnIce" in ix else ""}
                else:
                    gl, ast = g("goals"), g("assists")
                    out["players"][aid] = {"hpt": gl + ast, "hast": ast, "sog": g("shotsTotal"), "hgl": 1 if gl > 0 else 0,
                                           "played": True, "min": str(vals[ix["timeOnIce"]]) if "timeOnIce" in ix else ""}
    return out
