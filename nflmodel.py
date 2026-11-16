"""Pal's NFL moneyline model: team vs team, who wins and how sure.

Every regular-season game from this season and last (ESPN team schedules) goes into team power ratings:
  home margin = rating(home) - rating(away) + home field + quarterback change
fitted by weighted least squares. Older games count less (half weight every 120 days), so last season matters
early in the year and fades. Blowouts are capped at 24 points so one 40-point game doesn't decide a rating.

Quarterback change: in each past game, if the team's usual starter (main passer in 2+ of its last 4 games) didn't
start, the gap in passing quality (yards + 20 per TD, per attempt, over two years, pulled toward a backup level of
4.5 for thin histories) between him and whoever did start is a feature; the fit learns about 2 points per yard of
gap. So ratings are full-strength levels, and tonight's starter being ruled out is taken off on top.

Tested walk-forward (each game predicted only from games before it), 2024 season to now: 66.7% right overall,
against 65.2% for "better record wins" and 52% for "home team wins". The QB part made the win chances clearly
more accurate in 2024 and 2026 (about even in 2025). Bye weeks and short weeks were tested and didn't help.
Pure Python on purpose: no numpy on the host.
"""
import datetime as dt
import math
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

import requests

from nbamodel import _solve, american  # noqa: F401  (american is used by moneyline.py)

ET = ZoneInfo("America/New_York")
SITE = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"
H = {"User-Agent": "Mozilla/5.0 ParlayPal"}
HALF_LIFE = 120     # days until a game counts half
RIDGE = 1.0         # pulls thin-data teams toward average
CAP = 24            # blowout cap, points
SIGMA = 13.5        # points: how much NFL results spread around the predicted margin
MAX_P = 0.90
QK, QREPL = 200, 4.5   # quarterback quality: pseudo-attempts at backup level


def season_of(day):
    d = dt.date.fromisoformat(day)
    return d.year if d.month >= 3 else d.year - 1     # NFL seasons are named by the year they start


def key(a):
    return str(a or "").lower()


def _team_ids():
    j = requests.get(f"{SITE}/teams", timeout=20, headers=H).json()
    return [t["team"]["id"] for t in j["sports"][0]["leagues"][0]["teams"]]


def _schedule(tid, season):
    j = requests.get(f"{SITE}/teams/{tid}/schedule", params={"season": season, "seasontype": 2}, timeout=25, headers=H).json()
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
        out.append({"id": str(e.get("id")), "date": d.strftime("%Y-%m-%d"), "season": season, "h": key(h["team"]["abbreviation"]),
                    "a": key(a["team"]["abbreviation"]), "m": hs - as_, "neutral": bool(c.get("neutralSite"))})
    return out


def _num(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def _passers(gid):
    """{team: [{pid, name, att, yds, td}]} from one game's box score."""
    s = requests.get(f"{SITE}/summary", params={"event": gid}, timeout=25, headers=H).json()
    out = {}
    for team in (s.get("boxscore") or {}).get("players") or []:
        t = key((team.get("team") or {}).get("abbreviation"))
        rows = out.setdefault(t, [])
        for st in team.get("statistics") or []:
            if st.get("name") != "passing":
                continue
            for a in st.get("athletes") or []:
                v = dict(zip(st.get("keys") or [], a.get("stats") or []))
                att = (str(v.get("completions/passingAttempts", "0/0")).split("/") + ["0"])[1]
                ath = a.get("athlete") or {}
                rows.append({"pid": str(ath.get("id")), "name": ath.get("displayName") or "", "att": _num(att),
                             "yds": _num(v.get("passingYards")), "td": _num(v.get("passingTouchdowns"))})
    return out


def season_games(season):
    """Every finished regular-season game of one season, oldest first, with each side's passers."""
    games = {}
    with ThreadPoolExecutor(10) as ex:
        for rows in ex.map(lambda t: _schedule(t, season), _team_ids()):
            for g in rows:
                games[g["id"]] = g
    gl = sorted(games.values(), key=lambda g: g["date"])

    def box(g):
        try:
            return _passers(g["id"])
        except Exception:
            try:
                return _passers(g["id"])
            except Exception:
                return {}
    with ThreadPoolExecutor(12) as ex:
        for g, b in zip(gl, ex.map(box, gl)):
            g["qb"] = b
    return gl


def load(cached, day):
    s = season_of(day)
    prev, _ = cached(f"nflmodel:season:{s - 1}", 7 * 86400, lambda: season_games(s - 1))
    cur, _ = cached(f"nflmodel:season:{s}", 3 * 3600, lambda: season_games(s))
    return prev + cur


class QBs:
    """Who started at quarterback in each game, and how good each passer has been."""
    def __init__(self, games):
        self.tg, self.log, self.name = {}, {}, {}
        for g in games:
            self.tg.setdefault(g["h"], []).append(g)
            self.tg.setdefault(g["a"], []).append(g)
            for rows in (g.get("qb") or {}).values():
                for r in rows:
                    if r["att"] > 0:
                        self.log.setdefault(r["pid"], []).append((g["date"], r))
                        self.name[r["pid"]] = r["name"]
        for g in games:
            for side, t in (("qh", g["h"]), ("qa", g["a"])):
                g[side] = self.gap(g, t)

    @staticmethod
    def main(g, t):
        rows = (g.get("qb") or {}).get(t) or []
        r = max(rows, key=lambda r: r["att"], default=None)
        return r["pid"] if r and r["att"] >= 10 else None

    def qual(self, pid, day):
        lo = (dt.date.fromisoformat(day) - dt.timedelta(days=730)).isoformat()
        v = [r for d, r in self.log.get(pid, []) if lo <= d < day]
        att = sum(r["att"] for r in v)
        return (sum(r["yds"] + 20 * r["td"] for r in v) + QREPL * QK) / (att + QK)

    def usual(self, t, day):
        prior = [g for g in self.tg.get(t, []) if g["date"] < day][-4:]
        cnt = {}
        for g in prior:
            s = self.main(g, t)
            if s:
                cnt[s] = cnt.get(s, 0) + 1
        if not cnt:
            return None
        u = max(cnt, key=cnt.get)
        return u if cnt[u] >= 2 else None

    def gap(self, g, t):
        u, now = self.usual(t, g["date"]), self.main(g, t)
        if not u or not now or u == now:
            return 0.0
        return self.qual(u, g["date"]) - self.qual(now, g["date"])

    def backup(self, t, day, skip):
        """The team's next passer: most attempts for this team this year, not ruled out."""
        lo = (dt.date.fromisoformat(day) - dt.timedelta(days=365)).isoformat()
        att = {}
        for g in self.tg.get(t, []):
            if lo <= g["date"] < day:
                for r in (g.get("qb") or {}).get(t) or []:
                    if r["pid"] not in skip:
                        att[r["pid"]] = att.get(r["pid"], 0) + r["att"]
        return max(att, key=att.get) if att else None


def fit(games, day):
    prior = [g for g in games if g["date"] < day]
    if len(prior) < 60:
        return None
    teams = sorted({g["h"] for g in prior} | {g["a"] for g in prior})
    ix = {t: i for i, t in enumerate(teams)}
    n = len(teams) + 2
    A = [[0.0] * n for _ in range(n)]
    b = [0.0] * n
    d0 = dt.date.fromisoformat(day)
    for g in prior:
        w = 0.5 ** ((d0 - dt.date.fromisoformat(g["date"])).days / HALF_LIFE)
        y = max(-CAP, min(CAP, g["m"]))
        x = {ix[g["h"]]: 1.0, ix[g["a"]]: -1.0}
        if not g["neutral"]:
            x[n - 2] = 1.0
        q = g.get("qa", 0.0) - g.get("qh", 0.0)
        if q:
            x[n - 1] = q
        for i, xi in x.items():
            b[i] += w * xi * y
            for j, xj in x.items():
                A[i][j] += w * xi * xj
    for i in range(len(teams)):
        A[i][i] += RIDGE
    A[n - 1][n - 1] += 1.0
    A[n - 2][n - 2] += 1e-6
    beta = _solve(A, b)
    return {t: beta[ix[t]] for t in teams}, beta[n - 2], beta[n - 1]


def chance(margin):
    p = 0.5 * (1 + math.erf(margin / SIGMA / math.sqrt(2)))
    return min(max(p, 1 - MAX_P), MAX_P)


def predict(games, day, matchups, qbs, outs):
    """matchups: [(away, home)]. outs: {team: set of ESPN athlete ids ruled out}."""
    f = fit(games, day)
    if not f:
        return {}, {}
    r, hca, kq = f
    kq = max(0.0, kq)
    out = {}
    for a, h in matchups:
        if a not in r or h not in r:
            continue
        inj = {}
        for t in (a, h):
            lst = []
            u = qbs.usual(t, day)
            gone = {str(x) for x in outs.get(t, ())}
            if u and u in gone:
                bk = qbs.backup(t, day, gone | {u})
                gap = qbs.qual(u, day) - (qbs.qual(bk, day) if bk else QREPL)
                if gap > 0:
                    lst.append([qbs.name.get(u, "Starting QB") + " (QB)", round(kq * gap, 1),
                                qbs.name.get(bk) if bk else None])
            inj[t] = lst
        ia, ih = sum(x[1] for x in inj[a]), sum(x[1] for x in inj[h])
        m = r[h] - r[a] + hca + ia - ih
        out[(a, h)] = {"ph": chance(m), "m": m, "b2b": {}, "inj": inj,
                       "parts": {"rating": {a: round(r[a], 1), h: round(r[h], 1)}, "hca": round(hca, 1),
                                 "rest": {a: 0, h: 0}, "injury": {a: round(-ia, 1), h: round(-ih, 1)}}}
    ranks = {t: i + 1 for i, t in enumerate(sorted(r, key=lambda t: -r[t]))}
    return out, {"rating": {t: round(v, 1) for t, v in r.items()}, "rank": ranks, "hca": round(hca, 1), "kinj": round(kq, 2)}


def record(games, day, days=None):
    """Walk-forward test on this season: each week picked only from games before it."""
    s = season_of(day)
    dl = sorted({g["date"] for g in games if g["season"] == s and g["date"] < day})
    if days:
        cut = (dt.date.fromisoformat(day) - dt.timedelta(days=days)).isoformat()
        dl = [d for d in dl if d >= cut]
    right = total = 0
    for d in dl:
        f = fit(games, d)
        if not f:
            continue
        r, hca, kq = f
        for g in games:
            if g["date"] != d or g["m"] == 0 or g["h"] not in r or g["a"] not in r:
                continue
            m = r[g["h"]] - r[g["a"]] + (0 if g["neutral"] else hca) + kq * (g.get("qa", 0.0) - g.get("qh", 0.0))
            total += 1
            right += (m > 0) == (g["m"] > 0)
    return {"right": right, "games": total}
