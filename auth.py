"""Parlay Pal accounts: sign up, sign in, email verification and password reset.

Passwords are stored only as salted hashes, and sign-in lives in an HttpOnly cookie the page's scripts can't read.

Setup (see ACCOUNTS-SETUP.txt):
- Storage: users.db next to this file, or the path in env AUTH_DB. Vercel and Render wipe their disks,
  so those hosts need AUTH_DB on lasting storage, or accounts stay off with a clear message.
- Email: env SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, MAIL_FROM, SITE_URL, or the same names in smtp_settings.json.
- auth_dev_mode.txt (local testing only): with no email set up, shows the link on screen instead of sending it.
  Never put this file on a live site, because anyone could then reset anyone's password.
"""
import base64
import hashlib
import json
import os
import re
import secrets
import smtplib
import sqlite3
import threading
import time
from email.message import EmailMessage

from flask import Blueprint, jsonify, make_response, redirect, request
from werkzeug.security import check_password_hash, generate_password_hash

bp = Blueprint("auth", __name__)
HERE = os.path.dirname(os.path.abspath(__file__))
COOKIE = "pp_session"
SESSION_DAYS = 30
VERIFY_HOURS = 24
RESET_HOURS = 1
MAX_PHOTO_BYTES = 200 * 1024
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
TEAM_RE = re.compile(r"^[A-Z]{3}$")
COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
DUMMY_HASH = generate_password_hash("not-a-real-password")
_hits = {}
_hits_lock = threading.Lock()


class StorageOff(Exception):
    pass


def pg_url():
    """A Postgres database (Neon, added in Vercel's Storage tab) keeps accounts on hosts that wipe their disks."""
    for k in ("DATABASE_URL", "POSTGRES_URL"):
        if os.environ.get(k):
            return os.environ[k].strip()
    # Vercel can add a name in front, like STORAGE_DATABASE_URL
    for k, v in sorted(os.environ.items()):
        if v and (k.endswith("_DATABASE_URL") or k.endswith("_POSTGRES_URL")) and "UNPOOLED" not in k:
            return v.strip()
    return ""


class _Rows:
    """Makes Postgres results look like sqlite3.Row results, so the routes below work unchanged."""
    def __init__(self, cur):
        self.cur = cur
        self.cols = [d[0] for d in cur.description] if cur.description else []

    def fetchone(self):
        r = self.cur.fetchone()
        return dict(zip(self.cols, r)) if r is not None else None

    def fetchall(self):
        return [dict(zip(self.cols, r)) for r in self.cur.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())


class _Pg:
    """The small part of sqlite3's connection that this file uses, on top of Postgres."""
    ready = False

    def __init__(self, url):
        import ssl
        from urllib.parse import unquote, urlparse
        import pg8000.dbapi
        u = urlparse(url)
        self.con = pg8000.dbapi.connect(user=unquote(u.username or ""), password=unquote(u.password or ""),
                                        host=u.hostname, port=u.port or 5432, database=(u.path or "/").lstrip("/") or "postgres",
                                        ssl_context=None if os.environ.get("PG_NO_SSL") else ssl.create_default_context(), timeout=15)  # PG_NO_SSL: test database only
        if not _Pg.ready:
            for sql in (
                "CREATE TABLE IF NOT EXISTS users(id SERIAL PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL UNIQUE, pw TEXT NOT NULL,"
                " verified INTEGER NOT NULL DEFAULT 0, start BIGINT NOT NULL, team TEXT NOT NULL DEFAULT '', color TEXT NOT NULL DEFAULT '',"
                " photo TEXT NOT NULL DEFAULT '', mascot TEXT NOT NULL DEFAULT '', locker TEXT NOT NULL DEFAULT '')",
                "CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, uid INTEGER NOT NULL, expires BIGINT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY, uid INTEGER NOT NULL, kind TEXT NOT NULL, expires BIGINT NOT NULL)",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS locker TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS billing TEXT NOT NULL DEFAULT ''",
            ):
                self.con.cursor().execute(sql)
            self.con.commit()
            _Pg.ready = True

    def execute(self, sql, params=()):
        cur = self.con.cursor()
        cur.execute(sql.replace("?", "%s"), tuple(params))
        return _Rows(cur)

    def commit(self):
        self.con.commit()

    def __enter__(self):
        return self

    def __exit__(self, kind, *_):
        try:
            if kind is None:
                self.con.commit()
            else:
                self.con.rollback()
        finally:
            self.con.close()


def db_path():
    p = os.environ.get("AUTH_DB", "").strip()
    if p:
        return p
    if os.environ.get("VERCEL") or os.environ.get("RENDER"):
        raise StorageOff()
    return os.path.join(HERE, "users.db")


def db():
    if pg_url():
        return _Pg(pg_url())
    con = sqlite3.connect(db_path(), timeout=10)
    con.row_factory = sqlite3.Row
    con.executescript("""
        CREATE TABLE IF NOT EXISTS users(
          id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL UNIQUE, pw TEXT NOT NULL,
          verified INTEGER NOT NULL DEFAULT 0, start INTEGER NOT NULL, team TEXT NOT NULL DEFAULT '', color TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, uid INTEGER NOT NULL, expires INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY, uid INTEGER NOT NULL, kind TEXT NOT NULL, expires INTEGER NOT NULL);
    """)
    have = {r["name"] for r in con.execute("PRAGMA table_info(users)")}
    for col in ("photo", "mascot", "locker", "billing"):
        if col not in have:
            con.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
    return con


def _h(tok):
    return hashlib.sha256(tok.encode()).hexdigest()


def _settings():
    out = {}
    p = os.path.join(HERE, "smtp_settings.json")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            out.update(json.load(f))
    for k in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "MAIL_FROM", "SITE_URL"):
        if os.environ.get(k):
            out[k] = os.environ[k]
    return out


def dev_mode():
    return os.path.exists(os.path.join(HERE, "auth_dev_mode.txt")) and request.remote_addr in ("127.0.0.1", "::1")


def _site_url(cfg):
    if cfg.get("SITE_URL"):
        return str(cfg["SITE_URL"]).rstrip("/")
    return request.host_url.rstrip("/") if dev_mode() else None


def _send(to, subject, text):
    cfg = _settings()
    port = int(cfg.get("SMTP_PORT") or 587)
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, cfg.get("MAIL_FROM") or cfg.get("SMTP_USER"), to
    msg.set_content(text)
    if port == 465:
        srv = smtplib.SMTP_SSL(cfg["SMTP_HOST"], port, timeout=15)
    else:
        srv = smtplib.SMTP(cfg["SMTP_HOST"], port, timeout=15)
        if cfg.get("SMTP_USER"):
            srv.starttls()
    with srv:
        if cfg.get("SMTP_USER"):
            srv.login(cfg["SMTP_USER"], cfg.get("SMTP_PASS", ""))
        srv.send_message(msg)


def _mail_link(con, user, kind):
    """Create a one-time link and email it. Returns the fields the page shows."""
    con.execute("DELETE FROM tokens WHERE uid=? AND kind=?", (user["id"], kind))
    tok = secrets.token_urlsafe(32)
    hours = VERIFY_HOURS if kind == "verify" else RESET_HOURS
    con.execute("INSERT INTO tokens VALUES(?,?,?,?)", (_h(tok), user["id"], kind, int(time.time()) + hours * 3600))
    con.commit()
    cfg = _settings()
    base = _site_url(cfg)
    link = f"{base}/api/auth/verify?token={tok}" if kind == "verify" else f"{base}/?reset={tok}"
    if not cfg.get("SMTP_HOST") or not base:
        if dev_mode():
            return {"emailSent": False, "mailProblem": "not_configured", "devLink": link}
        return {"emailSent": False, "mailProblem": "not_configured"}
    if kind == "verify":
        subject, body = "Verify your Parlay Pal account", f"Hi {user['name']},\n\nConfirm your email by opening this link:\n{link}\n\nIt works for {VERIFY_HOURS} hours. If you didn't make this account, you can ignore this email.\n"
    else:
        subject, body = "Reset your Parlay Pal password", f"Hi {user['name']},\n\nChoose a new password by opening this link:\n{link}\n\nIt works for {RESET_HOURS} hour. If you didn't ask for this, you can ignore this email and your password stays the same.\n"
    try:
        _send(user["email"], subject, body)
        return {"emailSent": True}
    except (OSError, smtplib.SMTPException):
        return {"emailSent": False, "mailProblem": "send_failed"}


# ---------- locker room: every Pal you can roll, and how often ----------
# chance is out of 100. The basketball is everyone's starter, so it isn't on the wheel.
MASCOTS = {
    "ball": 0, "baseball": 48.75, "dice": 25, "chip": 12, "coin": 6, "goat": 4, "eight": 2.5, "dad": 1, "super": 0.5, "spider": 0.25,
}
START_SPINS = 3          # a new account starts with these
DAILY_SPINS = 3          # topped back up to 3 free spins every day


def _today():
    import datetime as dt
    from zoneinfo import ZoneInfo
    return dt.datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")


def _locker(u):
    """{"owned": {key: count}, "spins": unused spins, "day": last day the daily spin was given}"""
    try:
        lk = json.loads(u["locker"] or "{}")
    except (TypeError, ValueError):
        lk = {}
    if not isinstance(lk, dict) or "owned" not in lk:
        lk = {"owned": {"ball": 1}, "spins": START_SPINS, "day": _today()}
        if u["mascot"] and u["mascot"] in MASCOTS:
            lk["owned"][u["mascot"]] = 1
    if "free" not in lk:                   # free spins still unused today (the rest of "spins" were bought)
        lk["free"] = min(int(lk.get("spins", 0)), DAILY_SPINS)
    if lk.get("day") != _today():          # a new day: the 3 free spins come back, bought spins stay
        lk["spins"] = int(lk.get("spins", 0)) + DAILY_SPINS - int(lk.get("free", 0))
        lk["free"] = DAILY_SPINS
        lk["day"] = _today()
    lk["owned"] = {k: int(v) for k, v in (lk.get("owned") or {}).items() if k in MASCOTS and int(v) > 0} or {"ball": 1}
    if _is_owner(u):                       # site owners always have every Pal
        for k in MASCOTS:
            lk["owned"][k] = max(1, lk["owned"].get(k, 0))
    return lk


def _is_owner(u):
    """Emails listed in the OWNER_EMAILS setting (comma separated, kept in Vercel, not in the code)."""
    owners = {e.strip().lower() for e in os.environ.get("OWNER_EMAILS", "").split(",") if e.strip()}
    try:
        return bool(owners) and str(u["email"]).strip().lower() in owners
    except (KeyError, IndexError, TypeError):
        return False


def _public(u):
    import pay
    b = pay.billing(u)
    return {"id": u["id"], "name": u["name"], "email": u["email"], "verified": bool(u["verified"]), "start": u["start"], "team": u["team"], "color": u["color"], "photo": u["photo"], "mascot": u["mascot"], "locker": _locker(u),
            "pro": pay.is_pro(u) or _is_owner(u), "proUntil": int(b.get("until", 0)), "proEnding": bool(b.get("cancelAtEnd")), "hasBilling": bool(b.get("customer")),
            "trial": b.get("status") == "trialing", "trialUsed": bool(b.get("trialUsed")), "owner": _is_owner(u)}


def _start_session(resp, con, uid):
    tok = secrets.token_urlsafe(32)
    con.execute("INSERT INTO sessions VALUES(?,?,?)", (_h(tok), uid, int(time.time()) + SESSION_DAYS * 86400))
    con.commit()
    secure = request.is_secure or request.headers.get("X-Forwarded-Proto") == "https"
    resp.set_cookie(COOKIE, tok, max_age=SESSION_DAYS * 86400, httponly=True, samesite="Lax", secure=secure, path="/")


def _current(con):
    tok = request.cookies.get(COOKIE)
    if not tok:
        return None
    return con.execute("SELECT u.* FROM sessions s JOIN users u ON u.id=s.uid WHERE s.token=? AND s.expires>?", (_h(tok), int(time.time()))).fetchone()


def _limited(kind, who, most, seconds):
    """True once this person has tried too many times recently."""
    now, key = time.time(), f"{kind}:{who}"
    with _hits_lock:
        recent = [t for t in _hits.get(key, []) if now - t < seconds]
        recent.append(now)
        _hits[key] = recent
        return len(recent) > most


def _err(msg, code=400):
    return jsonify({"error": msg}), code


def _body():
    return request.get_json(silent=True) or {}


def _pw_problem(p):
    if len(p) < 8:
        return "Use at least 8 characters for your password."
    if len(p) > 200:
        return "That password is too long."
    return None


@bp.errorhandler(StorageOff)
def _storage_off(_):
    return _err("Accounts aren't set up on this site yet.", 503)


@bp.before_request
def _json_only():
    # a form on another site can't send JSON, which blocks forged requests
    if request.method == "POST" and not request.is_json:
        return _err("Please send JSON.", 415)
    return None


@bp.route("/me")
def me():
    with db() as con:
        u = _current(con)
        return jsonify({"user": _public(u) if u else None})


@bp.route("/signup", methods=["POST"])
def signup():
    b = _body()
    name, email, pw = str(b.get("name", "")).strip()[:60], str(b.get("email", "")).strip().lower()[:120], str(b.get("password", ""))
    if _limited("signup", request.remote_addr, 10, 3600):
        return _err("Too many tries. Please wait a while and try again.", 429)
    if not name:
        return _err("Please enter your name.")
    if not EMAIL_RE.match(email):
        return _err("Please enter a valid email.")
    prob = _pw_problem(pw)
    if prob:
        return _err(prob)
    with db() as con:
        if con.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            return _err("There's already an account with this email. Try signing in.", 409)
        con.execute("INSERT INTO users(name,email,pw,start) VALUES(?,?,?,?)", (name, email, generate_password_hash(pw), int(time.time() * 1000)))
        u = con.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()   # works the same on sqlite and Postgres
        mail = _mail_link(con, u, "verify")
        resp = make_response(jsonify({"user": _public(u), **mail}))
        _start_session(resp, con, u["id"])
        return resp


@bp.route("/login", methods=["POST"])
def login():
    b = _body()
    email, pw = str(b.get("email", "")).strip().lower(), str(b.get("password", ""))
    if _limited("login", f"{request.remote_addr}:{email}", 8, 900):
        return _err("Too many tries. Please wait 15 minutes and try again.", 429)
    with db() as con:
        u = con.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        ok = check_password_hash(u["pw"] if u else DUMMY_HASH, pw)
        if not u or not ok:
            return _err("That email and password don't match.", 401)
        resp = make_response(jsonify({"user": _public(u)}))
        _start_session(resp, con, u["id"])
        return resp


@bp.route("/logout", methods=["POST"])
def logout():
    tok = request.cookies.get(COOKIE)
    with db() as con:
        if tok:
            con.execute("DELETE FROM sessions WHERE token=?", (_h(tok),))
            con.commit()
    resp = make_response(jsonify({"ok": True}))
    resp.delete_cookie(COOKIE, path="/")
    return resp


@bp.route("/resend", methods=["POST"])
def resend():
    with db() as con:
        u = _current(con)
        if not u:
            return _err("Please sign in first.", 401)
        if u["verified"]:
            return jsonify({"alreadyVerified": True})
        if _limited("resend", u["id"], 5, 3600):
            return _err("Too many emails sent. Please wait a while and try again.", 429)
        return jsonify(_mail_link(con, u, "verify"))


@bp.route("/verify")
def verify():
    tok = request.args.get("token", "")
    with db() as con:
        row = con.execute("SELECT uid FROM tokens WHERE token=? AND kind='verify' AND expires>?", (_h(tok), int(time.time()))).fetchone()
        if not row:
            return redirect("/?verified=0")
        con.execute("UPDATE users SET verified=1 WHERE id=?", (row["uid"],))
        con.execute("DELETE FROM tokens WHERE uid=? AND kind='verify'", (row["uid"],))
        con.commit()
    return redirect("/?verified=1")


@bp.route("/forgot", methods=["POST"])
def forgot():
    email = str(_body().get("email", "")).strip().lower()
    if not EMAIL_RE.match(email):
        return _err("Please enter a valid email.")
    if _limited("forgot", request.remote_addr, 5, 3600):
        return _err("Too many tries. Please wait a while and try again.", 429)
    out = {"ok": True}
    with db() as con:
        u = con.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if u:
            mail = _mail_link(con, u, "reset")
            if "devLink" in mail:
                out["devLink"] = mail["devLink"]
    return jsonify(out)  # the same answer either way, so this can't reveal who has an account


@bp.route("/reset", methods=["POST"])
def reset():
    b = _body()
    tok, pw = str(b.get("token", "")), str(b.get("password", ""))
    prob = _pw_problem(pw)
    if prob:
        return _err(prob)
    with db() as con:
        row = con.execute("SELECT uid FROM tokens WHERE token=? AND kind='reset' AND expires>?", (_h(tok), int(time.time()))).fetchone()
        if not row:
            return _err("This reset link has expired. Please ask for a new one.")
        con.execute("UPDATE users SET pw=?, verified=1 WHERE id=?", (generate_password_hash(pw), row["uid"]))  # the link proved they own the email
        con.execute("DELETE FROM tokens WHERE uid=?", (row["uid"],))
        con.execute("DELETE FROM sessions WHERE uid=?", (row["uid"],))
        u = con.execute("SELECT * FROM users WHERE id=?", (row["uid"],)).fetchone()
        resp = make_response(jsonify({"user": _public(u)}))
        _start_session(resp, con, u["id"])
        return resp


@bp.route("/profile", methods=["POST"])
def profile():
    b = _body()
    name, email = str(b.get("name", "")).strip()[:60], str(b.get("email", "")).strip().lower()[:120]
    team, color = str(b.get("team", "")), str(b.get("color", ""))
    if not name:
        return _err("Please add a display name.")
    if not EMAIL_RE.match(email):
        return _err("Please enter a valid email address.")
    team = team if TEAM_RE.match(team) else ""
    color = color if COLOR_RE.match(color) else ""
    with db() as con:
        u = _current(con)
        if not u:
            return _err("Please sign in first.", 401)
        changed = email != u["email"]
        if changed and con.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            return _err("Another account already uses that email.", 409)
        con.execute("UPDATE users SET name=?, email=?, team=?, color=?, verified=? WHERE id=?", (name, email, team, color, 0 if changed else u["verified"], u["id"]))
        con.commit()
        u = con.execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone()
        out = {"user": _public(u)}
        if changed:
            out.update(_mail_link(con, u, "verify"))
        return jsonify(out)


@bp.route("/photo", methods=["POST"])
def photo():
    if (request.content_length or 0) > MAX_PHOTO_BYTES * 2:
        return _err("That picture is too large.", 413)
    data = _body().get("photo")
    if data:
        head = "data:image/jpeg;base64,"
        try:
            if not str(data).startswith(head):
                raise ValueError
            raw = base64.b64decode(str(data)[len(head):], validate=True)
        except ValueError:
            return _err("Please choose a JPG, PNG or WebP image.")
        # JPEG only, checked by its bytes: never store anything a browser could run
        if len(raw) > MAX_PHOTO_BYTES or raw[:3] != b"\xff\xd8\xff":
            return _err("That picture couldn't be used. Please try another.")
    with db() as con:
        u = _current(con)
        if not u:
            return _err("Please sign in first.", 401)
        con.execute("UPDATE users SET photo=? WHERE id=?", (str(data) if data else "", u["id"]))
        con.commit()
        return jsonify({"user": _public(con.execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone())})


@bp.route("/spin", methods=["POST"])
def spin():
    """Roll the wheel on the server, so a rare Pal can't be faked from the page."""
    with db() as con:
        u = _current(con)
        if not u:
            return _err("Please sign in first.", 401)
        if _limited("spin", u["id"], 30, 60):
            return _err("Slow down a little and try again.", 429)
        lk = _locker(u)
        if lk["spins"] <= 0:
            return _err("No spins left today. You get 3 free spins every day.", 409)
        pool = [(k, w) for k, w in MASCOTS.items() if w > 0]
        roll, got = secrets.randbelow(10 ** 6) / 10 ** 6 * sum(w for _, w in pool), pool[-1][0]
        for k, w in pool:
            if roll < w:
                got = k
                break
            roll -= w
        new = got not in lk["owned"]
        lk["owned"][got] = lk["owned"].get(got, 0) + 1
        lk["spins"] -= 1
        lk["free"] = max(0, int(lk.get("free", 0)) - 1)   # free ones get used first
        con.execute("UPDATE users SET locker=? WHERE id=?", (json.dumps(lk), u["id"]))
        con.commit()
        u = con.execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone()
        return jsonify({"got": got, "new": new, "user": _public(u)})


@bp.route("/mascot", methods=["POST"])
def mascot():
    kind = str(_body().get("mascot", ""))
    if kind not in MASCOTS:
        return _err("Please pick one of the mascots.")
    with db() as con:
        u = _current(con)
        if not u:
            return _err("Please sign in first.", 401)
        if kind not in _locker(u)["owned"]:
            return _err("Roll the wheel to unlock that Pal first.", 403)
        con.execute("UPDATE users SET mascot=? WHERE id=?", (kind, u["id"]))
        con.commit()
        return jsonify({"user": _public(con.execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone())})


@bp.route("/password", methods=["POST"])
def password():
    b = _body()
    cur, new = str(b.get("current", "")), str(b.get("new", ""))
    prob = _pw_problem(new)
    if prob:
        return _err(prob)
    with db() as con:
        u = _current(con)
        if not u:
            return _err("Please sign in first.", 401)
        if _limited("password", u["id"], 8, 900):
            return _err("Too many tries. Please wait 15 minutes and try again.", 429)
        if not check_password_hash(u["pw"], cur):
            return _err("Your current password isn't right.", 401)
        con.execute("UPDATE users SET pw=? WHERE id=?", (generate_password_hash(new), u["id"]))
        con.execute("DELETE FROM sessions WHERE uid=?", (u["id"],))  # signs out other devices
        resp = make_response(jsonify({"ok": True}))
        _start_session(resp, con, u["id"])
        return resp


@bp.route("/delete", methods=["POST"])
def delete():
    pw = str(_body().get("password", ""))
    import social
    with social._db():      # makes sure the social tables exist before cleaning them up
        pass
    with db() as con:
        u = _current(con)
        if not u:
            return _err("Please sign in first.", 401)
        if _limited("delete", u["id"], 8, 900):
            return _err("Too many tries. Please wait 15 minutes and try again.", 429)
        if not check_password_hash(u["pw"], pw):
            return _err("That password isn't right.", 401)
        for table, col in (("sessions", "uid"), ("tokens", "uid"), ("users", "id")):
            con.execute(f"DELETE FROM {table} WHERE {col}=?", (u["id"],))
        for sql in ("DELETE FROM likes WHERE uid=?", "DELETE FROM comments WHERE uid=?", "DELETE FROM follows WHERE uid=? OR fid=?",
                    "DELETE FROM reports WHERE uid=?", "DELETE FROM posts WHERE uid=?"):
            con.execute(sql, (u["id"],) * sql.count("?"))
        con.commit()
    resp = make_response(jsonify({"ok": True}))
    resp.delete_cookie(COOKIE, path="/")
    return resp
