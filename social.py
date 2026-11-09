"""Social tab: people post their parlays (graded by the site from real box scores), screenshots of wins,
and talk trash. Likes, comments, follows, and profiles with a real win-loss record.

Records only count parlays posted from the tracker, because the site grades those itself from ESPN box
scores; screenshots can't be checked, so they never change anyone's record."""
import base64
import json
import os
import re
import time

import requests

from flask import Blueprint, jsonify, request

import auth

bp = Blueprint("social", __name__)
MAX_IMG = 260 * 1024        # bytes of JPEG per screenshot (kept small: the free database is 0.5 GB)
MAX_TEXT = 500
HIDE_AT = 3                 # reports before a post is hidden
STATS = ("pts", "reb", "ast", "tpm", "stl", "blk", "pra", "pr", "pa", "ra", "dd", "td",
         "pyd", "ptd", "cmp", "ryd", "car", "rec", "recy", "rry", "atd", "hpt", "hast", "sog", "hgl", "svs")
GIF_OK = re.compile(r"^https://media[0-9]?\.giphy\.com/media/[A-Za-z0-9/_.-]+\.(gif|webp)(\?[A-Za-z0-9=&_.-]*)?$")


def _giphy_key():
    return os.environ.get("GIPHY_API_KEY", "").strip()
_ready = set()


def _db():
    con = auth.db()
    key = "pg" if auth.pg_url() else auth.db_path()
    if key not in _ready:
        pk = "SERIAL PRIMARY KEY" if auth.pg_url() else "INTEGER PRIMARY KEY"
        big = "BIGINT" if auth.pg_url() else "INTEGER"
        for sql in (
            f"CREATE TABLE IF NOT EXISTS posts(id {pk}, uid INTEGER NOT NULL, t {big} NOT NULL, text TEXT NOT NULL DEFAULT '',"
            " img TEXT NOT NULL DEFAULT '', legs TEXT NOT NULL DEFAULT '', stake TEXT NOT NULL DEFAULT '', payout TEXT NOT NULL DEFAULT '',"
            f" app TEXT NOT NULL DEFAULT '', res TEXT NOT NULL DEFAULT '', checked {big} NOT NULL DEFAULT 0, reports INTEGER NOT NULL DEFAULT 0)",
            "CREATE TABLE IF NOT EXISTS likes(pid INTEGER NOT NULL, uid INTEGER NOT NULL, PRIMARY KEY(pid, uid))",
            f"CREATE TABLE IF NOT EXISTS comments(id {pk}, pid INTEGER NOT NULL, uid INTEGER NOT NULL, t {big} NOT NULL, text TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS follows(uid INTEGER NOT NULL, fid INTEGER NOT NULL, PRIMARY KEY(uid, fid))",
            "CREATE TABLE IF NOT EXISTS reports(pid INTEGER NOT NULL, uid INTEGER NOT NULL, PRIMARY KEY(pid, uid))",
        ):
            con.execute(sql)
        con.commit()
        for col in ("gif", "odds"):        # added after launch
            try:
                con.execute(f"ALTER TABLE posts ADD COLUMN {'IF NOT EXISTS ' if auth.pg_url() else ''}{col} TEXT NOT NULL DEFAULT ''")
                con.commit()
            except Exception:
                if auth.pg_url():
                    con.con.rollback()
        _ready.add(key)
    return con


def _err(msg, code=400):
    return jsonify({"error": msg}), code


@bp.errorhandler(auth.StorageOff)
def _storage_off(_):
    return _err("Social isn't set up on this site yet.", 503)


@bp.before_request
def _json_only():
    if request.method == "POST" and not request.is_json:
        return _err("Please send JSON.", 415)
    return None


def _me(con):
    return auth._current(con)


def _body():
    return request.get_json(silent=True) or {}


# ---------- grading (same rules as the tracker: a player who sits out is a void leg) ----------
def _grade(legs, games):
    out, live = [], False
    for l in legs:
        g = games.get(str(l.get("gid"))) or {}
        if not g or g.get("error"):
            out.append("wait")
            continue
        st = g.get("status")
        live = live or st == 2
        p = (g.get("players") or {}).get(str(l.get("pid")))
        v = p.get(l.get("s")) if p and p.get("played") is not False else None
        if v is None:
            out.append("void" if st == 3 else "wait")
            l["v"] = None
            continue
        l["v"] = v
        out.append("hit" if v >= l.get("bar", 0) else ("miss" if st == 3 else "wait"))
    for l, r in zip(legs, out):
        l["res"] = r
    real = [r for r in out if r != "void"]
    if "miss" in real:
        return "miss", live
    if real and all(r == "hit" for r in real):
        return "hit", live
    if not real and out and all(r == "void" for r in out):
        return "void", live
    return "wait", live


def _regrade(con, rows):
    """Check unsettled parlays against box scores, a few at a time and at most every 3 minutes each."""
    now = int(time.time())
    todo = [r for r in rows if r["legs"] and r["res"] in ("", "wait") and now - int(r["checked"] or 0) > 45][:10]
    if not todo:
        return
    gids = []
    for r in todo:
        for l in json.loads(r["legs"]):
            if l.get("gid") and str(l["gid"]) not in gids:
                gids.append(str(l["gid"]))
    try:
        import live
        games = live.many(gids)
    except Exception:
        return
    for r in todo:
        legs = json.loads(r["legs"])
        res, _ = _grade(legs, games)
        r["legs"], r["res"], r["checked"] = json.dumps(legs), res, now
        con.execute("UPDATE posts SET legs=?, res=?, checked=? WHERE id=?", (r["legs"], res, now, r["id"]))
    con.commit()


def _record(con, uid):
    w = l = 0
    staked = won = 0.0
    for r in con.execute("SELECT res, stake, payout, odds FROM posts WHERE uid=? AND legs<>'' AND res IN ('hit','miss')", (uid,)).fetchall():
        try:
            s = float(r["stake"] or 0)
        except ValueError:
            s = 0
        try:
            p = float(r["payout"] or 0)
            if not p and r["odds"]:          # no payout typed in: work it out from the odds
                o = float(r["odds"])
                p = s * (1 + o / 100 if o > 0 else 1 + 100 / -o)
        except ValueError:
            p = 0
        staked += s
        if r["res"] == "hit":
            w += 1
            won += p
        else:
            l += 1
    return {"w": w, "l": l, "profit": round(won - staked, 2) if staked else None}


def _author(con, uid, cache):
    if uid not in cache:
        u = con.execute("SELECT id, name, photo, color, mascot, start FROM users WHERE id=?", (uid,)).fetchone()
        if not u:
            cache[uid] = None
        else:
            cache[uid] = {"id": u["id"], "name": u["name"], "photo": u["photo"], "color": u["color"], "mascot": u["mascot"], "rec": _record(con, uid)}
    return cache[uid]


def _shape(con, rows, me, cache):
    ids = [r["id"] for r in rows]
    likes, mine, comments = {}, set(), {}
    if ids:
        q = ",".join("?" * len(ids))
        for r in con.execute(f"SELECT pid, COUNT(*) AS n FROM likes WHERE pid IN ({q}) GROUP BY pid", ids).fetchall():
            likes[r["pid"]] = r["n"]
        for r in con.execute(f"SELECT pid, COUNT(*) AS n FROM comments WHERE pid IN ({q}) GROUP BY pid", ids).fetchall():
            comments[r["pid"]] = r["n"]
        if me:
            mine = {r["pid"] for r in con.execute(f"SELECT pid FROM likes WHERE uid=? AND pid IN ({q})", [me["id"]] + ids).fetchall()}
    out = []
    for r in rows:
        a = _author(con, r["uid"], cache)
        if not a:
            continue
        out.append({"id": r["id"], "t": r["t"], "text": r["text"], "img": bool(r["img"]), "legs": json.loads(r["legs"]) if r["legs"] else None,
                    "stake": r["stake"], "payout": r["payout"], "app": r["app"], "res": r["res"], "by": a, "gif": r.get("gif") or "", "odds": r.get("odds") or "",
                    "likes": likes.get(r["id"], 0), "liked": r["id"] in mine, "comments": comments.get(r["id"], 0),
                    "mine": bool(me and me["id"] == r["uid"])})
    return out


COLS = "id, uid, t, text, CASE WHEN img<>'' THEN 'y' ELSE '' END AS img, legs, stake, payout, app, res, checked, gif, odds"


@bp.route("/feed")
def feed():
    tab = request.args.get("tab", "new")
    try:
        before = int(request.args.get("before") or 0)
    except ValueError:
        before = 0
    with _db() as con:
        me = _me(con)
        where, args = ["reports<?"], [HIDE_AT]
        if before:
            where.append("id<?")
            args.append(before)
        if tab == "following":
            if not me:
                return jsonify({"posts": [], "needLogin": True})
            where.append("(uid IN (SELECT fid FROM follows WHERE uid=?) OR uid=?)")
            args += [me["id"], me["id"]]
        if tab == "wins":
            where.append("res='hit'")
        if tab == "top":
            where.append("t>?")
            args.append(int(time.time()) - 3 * 86400)
            rows = [dict(r) for r in con.execute(f"SELECT {COLS} FROM posts WHERE {' AND '.join(where)} ORDER BY "
                               "(SELECT COUNT(*) FROM likes WHERE pid=posts.id) + 2*(SELECT COUNT(*) FROM comments WHERE pid=posts.id) DESC, id DESC LIMIT 20", args).fetchall()]
        else:
            rows = [dict(r) for r in con.execute(f"SELECT {COLS} FROM posts WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT 20", args).fetchall()]
        _regrade(con, rows)
        return jsonify({"posts": _shape(con, rows, me, {}), "me": me["id"] if me else None, "gifs": bool(_giphy_key())})


@bp.route("/img/<int:pid>")
def img(pid):
    with _db() as con:
        r = con.execute("SELECT img FROM posts WHERE id=? AND reports<?", (pid, HIDE_AT)).fetchone()
    if not r or not r["img"]:
        return _err("No picture.", 404)
    raw = base64.b64decode(r["img"])
    return raw, 200, {"Content-Type": "image/jpeg", "Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff"}


def _clean_legs(legs):
    out = []
    for l in (legs or [])[:12]:
        if not isinstance(l, dict) or l.get("s") not in STATS:
            continue
        try:
            o = l.get("o")
            o = int(o) if isinstance(o, (int, float)) and 100 <= abs(o) <= 100000 else None
            out.append({"n": str(l.get("n", ""))[:40], "t": str(l.get("t", ""))[:4], "opp": str(l.get("opp", ""))[:4], "s": l["s"], "bar": float(l.get("bar", 0)),
                        "pid": str(l.get("pid", ""))[:16], "gid": str(l.get("gid", ""))[:16], "o": o, "est": bool(l.get("est")), "res": "wait", "v": None})
        except (TypeError, ValueError):
            continue
    return out


def _money(v):
    try:
        f = round(float(str(v).replace("$", "").replace(",", "")), 2)
    except ValueError:
        return ""
    return str(f) if 0 < f < 1_000_000 else ""


@bp.route("/post", methods=["POST"])
def post():
    if (request.content_length or 0) > MAX_IMG * 2:
        return _err("That picture is too large.", 413)
    b = _body()
    text = str(b.get("text", "")).strip()[:MAX_TEXT]
    legs = _clean_legs(b.get("legs"))
    img = ""
    if b.get("img"):
        head = "data:image/jpeg;base64,"
        try:
            if not str(b["img"]).startswith(head):
                raise ValueError
            raw = base64.b64decode(str(b["img"])[len(head):], validate=True)
        except ValueError:
            return _err("Please choose a JPG, PNG or WebP picture.")
        if len(raw) > MAX_IMG or raw[:3] != b"\xff\xd8\xff":
            return _err("That picture couldn't be used. Please try another.")
        img = base64.b64encode(raw).decode()
    gif = str(b.get("gif") or "")
    if gif and not GIF_OK.match(gif):
        return _err("That GIF couldn't be used.")
    odds = b.get("odds")
    odds = str(int(odds)) if isinstance(odds, (int, float)) and 100 <= abs(odds) <= 10_000_000 else ""
    if not odds and legs and all(l["o"] for l in legs):
        d = 1.0
        for l in legs:
            d *= 1 + l["o"] / 100 if l["o"] > 0 else 1 + 100 / -l["o"]
        odds = str(round((d - 1) * 100) if d >= 2 else round(-100 / (d - 1)))
    if not (text or legs or img or gif):
        return _err("Write something, add a parlay or add a picture.")
    with _db() as con:
        me = _me(con)
        if not me:
            return _err("Make a free account to post.", 401)
        if auth._limited("post", me["id"], 12, 3600):
            return _err("That's a lot of posts. Take a breather and try again in a bit.", 429)
        con.execute("INSERT INTO posts(uid, t, text, img, legs, stake, payout, app, res, gif, odds) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (me["id"], int(time.time()), text, img, json.dumps(legs) if legs else "", _money(b.get("stake")),
                     _money(b.get("payout")), str(b.get("app", ""))[:20], "wait" if legs else "", gif, odds))
        con.commit()
    return jsonify({"ok": True})


@bp.route("/like", methods=["POST"])
def like():
    pid = int(_body().get("id") or 0)
    with _db() as con:
        me = _me(con)
        if not me:
            return _err("Make a free account to like posts.", 401)
        if con.execute("SELECT 1 FROM likes WHERE pid=? AND uid=?", (pid, me["id"])).fetchone():
            con.execute("DELETE FROM likes WHERE pid=? AND uid=?", (pid, me["id"]))
            on = False
        else:
            con.execute("INSERT INTO likes(pid, uid) VALUES(?,?)", (pid, me["id"]))
            on = True
        n = con.execute("SELECT COUNT(*) AS n FROM likes WHERE pid=?", (pid,)).fetchone()["n"]
        con.commit()
    return jsonify({"liked": on, "likes": n})


@bp.route("/comments")
def comments():
    pid = int(request.args.get("id") or 0)
    with _db() as con:
        me = _me(con)
        rows = con.execute("SELECT id, uid, t, text FROM comments WHERE pid=? ORDER BY id LIMIT 200", (pid,)).fetchall()
        cache = {}
        out = [{"id": r["id"], "t": r["t"], "text": r["text"], "by": _author(con, r["uid"], cache), "mine": bool(me and me["id"] == r["uid"])}
               for r in rows]
    return jsonify({"comments": [c for c in out if c["by"]]})


@bp.route("/comment", methods=["POST"])
def comment():
    b = _body()
    pid, text = int(b.get("id") or 0), str(b.get("text", "")).strip()[:300]
    if not text:
        return _err("Write a comment first.")
    with _db() as con:
        me = _me(con)
        if not me:
            return _err("Make a free account to comment.", 401)
        if auth._limited("comment", me["id"], 40, 3600):
            return _err("Slow down a little, then try again.", 429)
        if not con.execute("SELECT 1 FROM posts WHERE id=?", (pid,)).fetchone():
            return _err("That post is gone.", 404)
        con.execute("INSERT INTO comments(pid, uid, t, text) VALUES(?,?,?,?)", (pid, me["id"], int(time.time()), text))
        con.commit()
    return jsonify({"ok": True})


@bp.route("/delete", methods=["POST"])
def delete():
    b = _body()
    pid, cid = int(b.get("id") or 0), int(b.get("cid") or 0)
    with _db() as con:
        me = _me(con)
        if not me:
            return _err("Please sign in first.", 401)
        boss = auth._is_owner(me)
        if cid:
            r = con.execute("SELECT uid FROM comments WHERE id=?", (cid,)).fetchone()
            if r and (r["uid"] == me["id"] or boss):
                con.execute("DELETE FROM comments WHERE id=?", (cid,))
        else:
            r = con.execute("SELECT uid FROM posts WHERE id=?", (pid,)).fetchone()
            if r and (r["uid"] == me["id"] or boss):
                for sql in ("DELETE FROM likes WHERE pid=?", "DELETE FROM comments WHERE pid=?", "DELETE FROM reports WHERE pid=?", "DELETE FROM posts WHERE id=?"):
                    con.execute(sql, (pid,))
        con.commit()
    return jsonify({"ok": True})


@bp.route("/report", methods=["POST"])
def report():
    pid = int(_body().get("id") or 0)
    with _db() as con:
        me = _me(con)
        if not me:
            return _err("Please sign in first.", 401)
        if not con.execute("SELECT 1 FROM reports WHERE pid=? AND uid=?", (pid, me["id"])).fetchone():
            con.execute("INSERT INTO reports(pid, uid) VALUES(?,?)", (pid, me["id"]))
            con.execute("UPDATE posts SET reports=reports+1 WHERE id=?", (pid,))
            if auth._is_owner(me):          # the owner's report hides it right away
                con.execute("UPDATE posts SET reports=? WHERE id=?", (HIDE_AT, pid))
            con.commit()
    return jsonify({"ok": True})


@bp.route("/follow", methods=["POST"])
def follow():
    fid = int(_body().get("uid") or 0)
    with _db() as con:
        me = _me(con)
        if not me:
            return _err("Make a free account to follow people.", 401)
        if fid == me["id"]:
            return _err("You can't follow yourself.")
        if con.execute("SELECT 1 FROM follows WHERE uid=? AND fid=?", (me["id"], fid)).fetchone():
            con.execute("DELETE FROM follows WHERE uid=? AND fid=?", (me["id"], fid))
            on = False
        else:
            con.execute("INSERT INTO follows(uid, fid) VALUES(?,?)", (me["id"], fid))
            on = True
        con.commit()
    return jsonify({"following": on})


@bp.route("/user/<int:uid>")
def user(uid):
    with _db() as con:
        me = _me(con)
        u = con.execute("SELECT id, name, photo, color, mascot, start FROM users WHERE id=?", (uid,)).fetchone()
        if not u:
            return _err("That person isn't here anymore.", 404)
        rows = [dict(r) for r in con.execute(f"SELECT {COLS} FROM posts WHERE uid=? AND reports<? ORDER BY id DESC LIMIT 30", (uid, HIDE_AT)).fetchall()]
        _regrade(con, rows)
        rec = _record(con, uid)
        cnt = lambda sql: con.execute(sql, (uid,)).fetchone()["n"]
        streak = 0
        for r in con.execute("SELECT res FROM posts WHERE uid=? AND legs<>'' AND res IN ('hit','miss') ORDER BY id DESC LIMIT 30", (uid,)).fetchall():
            if streak == 0:
                streak = 1 if r["res"] == "hit" else -1
            elif (streak > 0) == (r["res"] == "hit"):
                streak += 1 if streak > 0 else -1
            else:
                break
        return jsonify({"user": {"id": u["id"], "name": u["name"], "photo": u["photo"], "color": u["color"], "mascot": u["mascot"], "start": u["start"],
                                 "rec": rec, "streak": streak, "posts": cnt("SELECT COUNT(*) AS n FROM posts WHERE uid=?"),
                                 "followers": cnt("SELECT COUNT(*) AS n FROM follows WHERE fid=?"), "following": cnt("SELECT COUNT(*) AS n FROM follows WHERE uid=?"),
                                 "iFollow": bool(me and con.execute("SELECT 1 FROM follows WHERE uid=? AND fid=?", (me["id"], uid)).fetchone()),
                                 "me": bool(me and me["id"] == uid)},
                        "posts": _shape(con, rows, me, {})})


@bp.route("/leaders")
def leaders():
    """Best records this week (at least 3 graded parlays)."""
    since = int(time.time()) - 7 * 86400
    with _db() as con:
        rows = con.execute("SELECT uid, SUM(CASE WHEN res='hit' THEN 1 ELSE 0 END) AS w, SUM(CASE WHEN res='miss' THEN 1 ELSE 0 END) AS l "
                           "FROM posts WHERE legs<>'' AND res IN ('hit','miss') AND t>? GROUP BY uid", (since,)).fetchall()
        rows = [r for r in rows if r["w"] + r["l"] >= 3]
        rows.sort(key=lambda r: (-(r["w"] / (r["w"] + r["l"])), -r["w"]))
        cache, out = {}, []
        for r in rows[:10]:
            a = _author(con, r["uid"], cache)
            if a:
                out.append({"by": a, "w": r["w"], "l": r["l"]})
    return jsonify({"leaders": out})


@bp.route("/gifs")
def gifs():
    """GIF search through GIPHY (key in Vercel env GIPHY_API_KEY). Trending when there's no search."""
    key = _giphy_key()
    if not key:
        return jsonify({"configured": False, "gifs": []})
    q = str(request.args.get("q", "")).strip()[:50]
    path = "search" if q else "trending"
    try:
        off = max(0, min(4900, int(request.args.get("offset") or 0)))
    except ValueError:
        off = 0
    params = {"api_key": key, "limit": 48, "offset": off, "rating": "pg-13", "bundle": "messaging_non_clips"}
    if q:
        params["q"] = q
    try:
        r = requests.get(f"https://api.giphy.com/v1/gifs/{path}", params=params, timeout=10)
        body = r.json()
        data = body.get("data") or []
        pg = body.get("pagination") or {}
    except (requests.RequestException, ValueError):
        return _err("GIFs aren't loading right now.", 502)
    out = []
    for g in data:
        im = (g.get("images") or {})
        small, full = im.get("fixed_width") or {}, im.get("downsized_medium") or im.get("original") or {}
        u, f = small.get("webp") or small.get("url"), full.get("url")
        if u and f and GIF_OK.match(u) and GIF_OK.match(f):
            out.append({"id": g.get("id"), "s": u, "u": f, "w": int(small.get("width") or 200), "h": int(small.get("height") or 150)})
    more = (pg.get("offset", off) + pg.get("count", len(data))) < min(pg.get("total_count", 0), 4999)
    return jsonify({"configured": True, "gifs": out, "more": bool(more and data)})


@bp.route("/oops", methods=["POST"])
def oops():
    """The app reports requests that never reached the server, so they show up in Vercel's logs."""
    b = _body()
    print("SOCIAL CLIENT ERROR", json.dumps({k: str(b.get(k, ""))[:300] for k in ("path", "err", "size", "ua", "url", "try")}), flush=True)
    return jsonify({"ok": True})
