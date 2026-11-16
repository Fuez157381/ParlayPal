"""Pal's NBA moneyline model: who wins tonight, and how sure.

Every regular-season game from this season and last (ESPN team schedules) goes into team power ratings:
  home margin = rating(home) - rating(away) + home court + back-to-back + who sat out
fitted by weighted least squares. "Who sat out": for each game, the regulars (5+ of the team's last 10 games,
20+ minutes) who didn't play, valued by their box-score production; the fit learns how many points that costs.
So a team's rating is its full-strength level, and tonight's injury report is taken off on top. Older games count less (half weight every 90 days), so last season matters
early in the year and fades as this season's games pile up. A predicted margin becomes a win chance with a
normal curve (spread 12 points), which matched real results when tested.

Tested walk-forward (each game predicted only from games before it): about 69-70% right on 2025-26
with injuries (68% without), 69% early in 2026-27, versus 67% for "better record wins" and 55% for "home team wins".
Head-to-head history was tested too and made it slightly worse (ratings already capture it), so it's shown, not used.
Pure Python on purpose: no numpy on the host.
"""
import datetime as dt
import math
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

import requests

ET = ZoneInfo("America/New_York")
SITE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
HALF_LIFE = 90      # days until a game counts half
RIDGE = 5.0         # pulls thin-data teams toward average
SIGMA = 12.0        # points: how much NBA results spread around the predicted margin
MAX_P = 0.92        # tested: picks above about 90% won a bit less often than predicted
REPL = 9.0          # box value a bench replacement gives; only production above this counts as lost
MIN_MIN = 20        # minutes a regular averages


def _season_of(day):
    d = dt.date.fromisoformat(day)
    return d.year + 1 if d.month >= 8 else d.year      # ESPN names a season by the year it ends


def _team_ids():
    j = requests.get(f"{SITE}/teams", timeout=20).json()
    return [(t["team"]["id"], t["team"]["abbreviation"]) for t in j["sports"][0]["leagues"][0]["teams"]]


def _schedule(tid, season):
    j = requests.get(f"{SITE}/teams/{tid}/schedule", params={"season": season, "seasontype": 2}, timeout=25,
                     headers={"User-Agent": "Mozilla/5.0 ParlayPal"}).json()
    out = []
    for e in j.get("events") or []:
        c = (e.get("competitions") or [{}])[0]
        if not ((c.get("status") or {}).get("type") or {}).get("completed"):
            continue
        cs = c.get("competitors") or []
        h = next((x for x in cs if x.get("homeAway") == "home"), None)
        a = next((x for x in cs if x.get("homeAway") == "away"), None)
        if not h or not a:
            continue

        def sc(x):
            v = x.get("score")
            v = v.get("value") if isinstance(v, dict) else v
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
        hs, as_ = sc(h), sc(a)
        if hs is None or as_ is None:
            continue
        d = dt.datetime.fromisoformat(c["date"].replace("Z", "+00:00")).astimezone(ET)
        out.append({"id": e.get("id"), "date": d.strftime("%Y-%m-%d"), "season": season, "h": h["team"]["abbreviation"],
                    "a": a["team"]["abbreviation"], "m": hs - as_, "neutral": bool(c.get("neutralSite"))})
    return out


def season_games(season):
    """Every finished regular-season game of one season, oldest first (one copy of each)."""
    ids = _team_ids()
    games = {}
    with ThreadPoolExecutor(10) as ex:
        for rows in ex.map(lambda t: _schedule(t[0], season), ids):
            for g in rows:
                games[g["id"]] = g
    return sorted(games.values(), key=lambda g: g["date"])


def load(cached, day):
    """This season and last, from cache when fresh."""
    s = _season_of(day)
    prev, _ = cached(f"nbamodel:season:{s - 1}", 7 * 86400, lambda: season_games(s - 1))
    cur, _ = cached(f"nbamodel:season:{s}", 3 * 3600, lambda: season_games(s))
    return prev + cur


def _bval(r):
    """One game's box-score value (no shooting splits in the logs)."""
    return r.get("pts", 0) + 0.4 * r.get("reb", 0) + 0.7 * r.get("ast", 0) + r.get("stl", 0) + 0.7 * r.get("blk", 0)


class Players:
    """Who played each game, and each player's recent value, from ESPN game logs (rows like espn.fetch_gamelog)."""
    def __init__(self, rows, tri=lambda a: a):
        self.played, self.log, self.name = {}, {}, {}
        for r in rows:
            self.played.setdefault((str(r["gid"]), r["team"]), set()).add(r["pid"])
            self.log.setdefault(r["pid"], []).append((r["date"], r.get("min", 0), _bval(r)))
            self.name[r["pid"]] = r.get("name") or str(r["pid"])
        for v in self.log.values():
            v.sort()

    def value(self, pid, day):
        v = [x for x in self.log.get(pid, []) if x[0] < day][-15:]
        if not v:
            return 0.0, 0.0
        return sum(x[2] for x in v) / len(v), sum(x[1] for x in v) / len(v)

    def regulars(self, team_games, team, day):
        """{pid: (value, minutes)} for the team's regulars going into `day`."""
        prior = [g for g in team_games if g["date"] < day][-10:]
        if len(prior) < 5:
            return {}
        cnt = {}
        for g in prior:
            for pid in self.played.get((str(g["id"]), team), ()):
                cnt[pid] = cnt.get(pid, 0) + 1
        out = {}
        for pid, n in cnt.items():
            if n >= 5:
                v, mn = self.value(pid, day)
                if mn >= MIN_MIN:
                    out[pid] = (v, mn)
        return out


def lost(v):
    return max(0.0, v - REPL)


def add_missing(games, players, tri=lambda a: a):
    """Sets g["mh"], g["ma"]: production missing for home and away in each past game (0 when unknown)."""
    tg = {}
    for g in games:
        tg.setdefault(tri(g["h"]), []).append(g)
        tg.setdefault(tri(g["a"]), []).append(g)
    for g in games:
        for side, t in (("mh", tri(g["h"])), ("ma", tri(g["a"]))):
            now = players.played.get((str(g["id"]), t))
            if not now:
                g[side] = 0.0
                continue
            reg = players.regulars(tg[t], t, g["date"])
            g[side] = sum(lost(v) for pid, (v, mn) in reg.items() if pid not in now)
    return tg


def _rest(games):
    """Days since each team's previous game, per game, as (home rest, away rest)."""
    last, out = {}, {}
    for g in games:
        for side, t in (("rh", g["h"]), ("ra", g["a"])):
            p = last.get(t)
            out[(g["id"], side)] = (dt.date.fromisoformat(g["date"]) - dt.date.fromisoformat(p)).days if p else 9
            last[t] = g["date"]
    return out


def _solve(A, b):
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(M[r][c]))
        M[c], M[p] = M[p], M[c]
        if abs(M[c][c]) < 1e-12:
            continue
        for r in range(n):
            if r != c and M[r][c]:
                f = M[r][c] / M[c][c]
                for k in range(c, n + 1):
                    M[r][k] -= f * M[c][k]
    return [M[i][n] / M[i][i] if abs(M[i][i]) > 1e-12 else 0.0 for i in range(n)]


def fit(games, day, rest=None):
    """Ratings from games played before `day`: ({team: points vs average at full strength}, home court,
    back-to-back cost, points per unit of missing production)."""
    prior = [g for g in games if g["date"] < day]
    teams = sorted({g["h"] for g in prior} | {g["a"] for g in prior})
    if len(prior) < 30:
        return None
    ix = {t: i for i, t in enumerate(teams)}
    n = len(teams) + 3
    A = [[0.0] * n for _ in range(n)]
    b = [0.0] * n
    rest = rest or _rest(games)
    d0 = dt.date.fromisoformat(day)
    for g in prior:
        w = 0.5 ** ((d0 - dt.date.fromisoformat(g["date"])).days / HALF_LIFE)
        x = {ix[g["h"]]: 1.0, ix[g["a"]]: -1.0}
        if not g["neutral"]:
            x[n - 3] = 1.0
        bb = (1 if rest[(g["id"], "ra")] <= 1 else 0) - (1 if rest[(g["id"], "rh")] <= 1 else 0)
        if bb:
            x[n - 2] = float(bb)
        mi = g.get("ma", 0.0) - g.get("mh", 0.0)
        if mi:
            x[n - 1] = mi
        for i, xi in x.items():
            b[i] += w * xi * g["m"]
            for j, xj in x.items():
                A[i][j] += w * xi * xj
    for i in range(len(teams)):
        A[i][i] += RIDGE
    A[n - 1][n - 1] += 1.0                      # keeps the injury weight steady when few games had absences
    beta = _solve(A, b)
    return {t: beta[ix[t]] for t in teams}, beta[n - 3], beta[n - 2], beta[n - 1]


def chance(margin):
    p = 0.5 * (1 + math.erf(margin / SIGMA / math.sqrt(2)))
    return min(max(p, 1 - MAX_P), MAX_P)


def american(p):
    """Fair American odds for a win chance (no sportsbook cut)."""
    if p >= 0.5:
        return -round(100 * p / (1 - p))
    return round(100 * (1 - p) / p)


def _last_game(games, team, day):
    for g in reversed(games):
        if g["date"] < day and team in (g["h"], g["a"]):
            return g["date"]
    return None


def predict(games, day, matchups, tri=lambda a: a, players=None, outs=None, tg=None):
    """matchups: [(away, home)] in page team codes. outs: {page team: set of player ids ruled out tonight}.
    Returns {(away, home): {...}} plus ratings."""
    f = fit(games, day)
    if not f:
        return {}, {}
    r, hca, b2b, kinj = f
    kinj = max(0.0, kinj)
    page = {}
    for t in r:
        page[tri(t)] = t
    out = {}
    d0 = dt.date.fromisoformat(day)
    for a, h in matchups:
        ea, eh = page.get(a), page.get(h)
        if ea is None or eh is None:
            continue
        ra_, rh_ = _last_game(games, ea, day), _last_game(games, eh, day)
        ba = 1 if ra_ and (d0 - dt.date.fromisoformat(ra_)).days <= 1 else 0
        bh = 1 if rh_ and (d0 - dt.date.fromisoformat(rh_)).days <= 1 else 0
        inj = {}
        for side, et in ((a, ea), (h, eh)):
            lst = []
            if players and outs and tg is not None:
                reg = players.regulars(tg.get(side, []), side, day)
                for pid in outs.get(side, ()):
                    if pid in reg and lost(reg[pid][0]) > 0:
                        lst.append([players.name.get(pid, str(pid)), round(kinj * lost(reg[pid][0]), 1)])
            inj[side] = sorted(lst, key=lambda x: -x[1])
        ia, ih = sum(x[1] for x in inj[a]), sum(x[1] for x in inj[h])
        m = r[eh] - r[ea] + hca + b2b * (ba - bh) + ia - ih
        p = chance(m)
        out[(a, h)] = {"ph": p, "m": m, "b2b": {a: ba, h: bh}, "inj": inj,
                       "parts": {"rating": {a: round(r[ea], 1), h: round(r[eh], 1)}, "hca": round(hca, 1),
                                 "rest": {a: round(-b2b * ba, 1), h: round(-b2b * bh, 1)},
                                 "injury": {a: round(-ia, 1), h: round(-ih, 1)}}}
    ranks = {tri(t): i + 1 for i, t in enumerate(sorted(r, key=lambda t: -r[t]))}
    return out, {"rating": {tri(t): round(v, 1) for t, v in r.items()}, "rank": ranks, "hca": round(hca, 1), "kinj": round(kinj, 3)}


def record(games, day, days=None):
    """Walk-forward test on this season: picks made only from games before each date."""
    s = _season_of(day)
    rest = _rest(games)
    days_list = sorted({g["date"] for g in games if g["season"] == s and g["date"] < day})
    if days:
        cut = (dt.date.fromisoformat(day) - dt.timedelta(days=days)).isoformat()
        days_list = [d for d in days_list if d >= cut]
    right = total = 0
    for d in days_list:
        f = fit(games, d, rest)
        if not f:
            continue
        r, hca, b2b, kinj = f
        for g in games:
            if g["date"] != d or g["h"] not in r or g["a"] not in r:
                continue
            bb = (1 if rest[(g["id"], "ra")] <= 1 else 0) - (1 if rest[(g["id"], "rh")] <= 1 else 0)
            m = r[g["h"]] - r[g["a"]] + (0 if g["neutral"] else hca) + b2b * bb + kinj * (g.get("ma", 0.0) - g.get("mh", 0.0))
            total += 1
            right += (m > 0) == (g["m"] > 0)
    return {"right": right, "games": total}
