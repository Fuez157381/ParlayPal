"""Payments with Stripe: Parlay Pal Pro ($12.99 a month, unlimited Pal credits), Parlay Pal Plus ($7.99 a month, 30 a day) and spin packs.

Switched on by one Vercel setting (env var):
  STRIPE_SECRET_KEY      sk_live_... for real money (sk_test_... only works with PAY_TEST=1)
Optional:
  STRIPE_WEBHOOK_SECRET  whsec_... from a webhook pointed at /api/pay/webhook, for instant updates
Without the key every route answers "payments aren't switched on yet" and nothing is charged.

Stripe's hosted checkout page takes the card, so card numbers never touch this site.
What someone bought is only trusted when Stripe itself says it was paid: either its signed webhook,
or this server asking Stripe directly (after checkout, and every few hours for subscriptions)."""
import hashlib
import hmac
import json
import os
import time

import requests
from flask import Blueprint, jsonify, request

import auth

bp = Blueprint("pay", __name__)
API = "https://api.stripe.com/v1"

TRIAL_DAYS = 0        # no free trial: new accounts are offered Pro, then Plus, then stay Free
TRIAL_SPINS = 10      # bonus spins for a first subscription

ITEMS = {
    "pro": {"name": "Parlay Pal Pro", "cents": 1299, "monthly": True},
    "plus": {"name": "Parlay Pal Plus", "cents": 799, "monthly": True},
    "spins10": {"name": "10 spins", "cents": 50, "spins": 10},
    "spins25": {"name": "25 spins", "cents": 100, "spins": 25},
}


def _key():
    return os.environ.get("STRIPE_SECRET_KEY", "").strip()


def _hook_secret():
    return os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()


def live():
    """Real payments only. Test keys (sk_test_) keep the paywall off, so nobody gets Pro with a fake test card,
    unless PAY_TEST=1 is set while testing."""
    k = _key()
    if not k:
        return False
    return not k.startswith(("sk_test_", "rk_test_")) or os.environ.get("PAY_TEST", "").strip() == "1"


def _site():
    s = os.environ.get("SITE_URL", "").strip().rstrip("/")
    return s or request.host_url.rstrip("/")


def _stripe(path, data=None, get=False):
    if get:
        r = requests.get(API + path, params=data, auth=(_key(), ""), timeout=15)
    else:
        r = requests.post(API + path, data=data, auth=(_key(), ""), timeout=20)
    j = r.json()
    if r.status_code >= 400:
        raise RuntimeError((j.get("error") or {}).get("message") or "Stripe said no.")
    return j


def billing(u):
    try:
        b = json.loads(u["billing"] or "{}")
    except (TypeError, ValueError, KeyError, IndexError):
        b = {}
    return b if isinstance(b, dict) else {}


def is_pro(u):
    b = billing(u)
    return b.get("status") in ("active", "trialing") and int(b.get("until", 0)) > time.time() - 3 * 86400


RANK = {"free": 0, "plus": 1, "pro": 2}


def plan(u):
    """free, plus or pro. Subscriptions from before Plus existed count as Pro."""
    if auth._is_owner(u):
        return "pro"
    if not is_pro(u):
        return "free"
    p = billing(u).get("plan")
    return p if p in ("plus", "pro") else "pro"


def _err(msg, code=400):
    return jsonify({"error": msg}), code


@bp.route("/config")
def config():
    return jsonify({"live": live(), "items": {k: {"name": v["name"], "cents": v["cents"]} for k, v in ITEMS.items()}})


@bp.route("/checkout", methods=["POST"])
def checkout():
    if not request.is_json:
        return _err("Please send JSON.", 415)
    if not live():
        return _err("Payments aren't switched on yet. Nothing was charged.", 503)
    item = str((request.get_json(silent=True) or {}).get("item", ""))
    it = ITEMS.get(item)
    if not it:
        return _err("Pick something to buy.")
    with auth.db() as con:
        u = auth._current(con)
        if not u:
            return _err("Please sign in first.", 401)
        if it.get("monthly") and RANK[plan(u)] >= RANK[item]:
            return _err(f"You already have {ITEMS[plan(u)]['name']}.", 409)
        b, site = billing(u), _site()
        data = {
            "mode": "subscription" if it.get("monthly") else "payment",
            "line_items[0][quantity]": 1,
            "line_items[0][price_data][currency]": "usd",
            "line_items[0][price_data][unit_amount]": it["cents"],
            "line_items[0][price_data][product_data][name]": it["name"],
            "client_reference_id": str(u["id"]),
            "metadata[uid]": str(u["id"]),
            "metadata[item]": item,
            "success_url": f"{site}/?paid={item}&cs={{CHECKOUT_SESSION_ID}}#home",
            "cancel_url": f"{site}/?paid=no#home",
        }
        if it.get("monthly"):
            data["line_items[0][price_data][recurring][interval]"] = "month"
            data["subscription_data[metadata][uid]"] = str(u["id"])
            data["subscription_data[metadata][item]"] = item
            data["payment_method_collection"] = "always"
            if TRIAL_DAYS and not b.get("trialUsed"):              # one free trial per account
                data["subscription_data[trial_period_days]"] = TRIAL_DAYS
                data["subscription_data[trial_settings][end_behavior][missing_payment_method]"] = "cancel"
        if b.get("customer"):
            data["customer"] = b["customer"]
        else:
            data["customer_email"] = u["email"]
            if not it.get("monthly"):
                data["customer_creation"] = "always"
        try:
            s = _stripe("/checkout/sessions", data)
        except (RuntimeError, requests.RequestException, ValueError) as e:
            return _err(f"Couldn't start checkout: {e}", 502)
        return jsonify({"url": s["url"]})


@bp.route("/portal", methods=["POST"])
def portal():
    """Stripe's own page to cancel Pro or change the card."""
    if not live():
        return _err("Payments aren't switched on yet.", 503)
    with auth.db() as con:
        u = auth._current(con)
        if not u:
            return _err("Please sign in first.", 401)
        cus = billing(u).get("customer")
        if not cus:
            return _err("There's no subscription on this account.", 404)
        args = {"customer": cus, "return_url": _site() + "/#home"}
        try:
            try:
                s = _stripe("/billing_portal/sessions", args)
            except RuntimeError as e:
                if "configuration" not in str(e).lower():
                    raise
                args["configuration"] = _portal_config()      # first time: set up Stripe's manage page for you
                s = _stripe("/billing_portal/sessions", args)
        except (RuntimeError, requests.RequestException, ValueError) as e:
            return _err(f"Couldn't open billing: {e}", 502)
        return jsonify({"url": s["url"]})


def _portal_config():
    """Stripe's page where people cancel, change their card and see receipts."""
    c = _stripe("/billing_portal/configurations", {
        "business_profile[headline]": "Manage your Parlay Pal plan",
        "features[subscription_cancel][enabled]": "true",
        "features[subscription_cancel][mode]": "at_period_end",
        "features[payment_method_update][enabled]": "true",
        "features[invoice_history][enabled]": "true",
        "features[customer_update][enabled]": "true",
        "features[customer_update][allowed_updates][0]": "email",
    })
    return c["id"]


def _verified(payload, header, secret, tolerance=300):
    parts = {}
    for kv in (header or "").split(","):
        k, _, v = kv.partition("=")
        parts.setdefault(k.strip(), []).append(v.strip())
    try:
        t = int(parts.get("t", ["0"])[0])
    except ValueError:
        return False
    if abs(time.time() - t) > tolerance:
        return False
    want = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(want, sig) for sig in parts.get("v1", []))


def _stripe_cancel(sub):
    """Ends an old subscription right away (Plus, after upgrading to Pro). Its unused days become credit on the next bill."""
    try:
        requests.delete(f"{API}/subscriptions/{sub}", data={"prorate": "true"}, auth=(_key(), ""), timeout=20)
    except requests.RequestException:
        pass


def _save(con, uid, fn):
    u = con.execute("SELECT * FROM users WHERE id=?", (int(uid),)).fetchone()
    if not u:
        return
    b = billing(u)
    lk = auth._locker(u)
    fn(b, lk)
    con.execute("UPDATE users SET billing=?, locker=? WHERE id=?", (json.dumps(b), json.dumps(lk), u["id"]))
    con.commit()


def _apply_checkout(con, uid, obj):
    """A finished checkout: give what was bought. Returns old subscriptions to end (after an upgrade)."""
    meta = obj.get("metadata") or {}
    item = ITEMS.get(meta.get("item", ""), {})
    old = []

    def paid(b, lk):
        seen = b.get("seen") or []
        if obj.get("id") in seen:      # Stripe can report the same checkout twice
            return
        b["seen"] = (seen + [obj.get("id")])[-40:]
        if obj.get("customer"):
            b["customer"] = obj["customer"]
        if item.get("spins"):
            lk["spins"] = int(lk.get("spins", 0)) + item["spins"]
        if item.get("monthly"):
            first = bool(TRIAL_DAYS) and not b.get("trialUsed")
            new = obj.get("subscription")
            if isinstance(new, dict):
                new = new.get("id")
            if b.get("sub") and new and b["sub"] != new and b.get("status") in ("active", "trialing"):
                old.append(b["sub"])          # upgrading: the old plan ends once this is saved
            b["sub"] = new
            b["plan"] = meta.get("item") if meta.get("item") in ("plus", "pro") else "pro"
            if first:
                b["status"] = "trialing"
                b["until"] = int(time.time()) + TRIAL_DAYS * 86400
            else:
                b["status"] = "active"
                b["until"] = max(int(b.get("until", 0)), int(time.time()) + 32 * 86400)
            if not b.get("trialSpins"):
                b["trialSpins"] = True
                lk["spins"] = int(lk.get("spins", 0)) + TRIAL_SPINS
            b["trialUsed"] = True
    _save(con, uid, paid)
    return old


def _apply_sub(con, uid, obj, deleted=False):
    """News about a subscription: renewed, canceled, card failed, and so on."""
    meta = obj.get("metadata") or {}

    def sub(b, lk):
        if b.get("sub") and obj.get("id") != b["sub"] and b.get("status") in ("active", "trialing"):
            return                         # news about an older plan this person already replaced
        b["sub"] = obj.get("id")
        if meta.get("item") in ("plus", "pro"):
            b["plan"] = meta["item"]
        if obj.get("customer"):
            b["customer"] = obj["customer"]
        b["status"] = "canceled" if deleted else obj.get("status", "")
        end = obj.get("current_period_end") or ((obj.get("items") or {}).get("data") or [{}])[0].get("current_period_end")
        if end:
            b["until"] = int(end)
        b["cancelAtEnd"] = bool(obj.get("cancel_at_period_end"))
        if obj.get("trial_end"):
            b["trialUsed"] = True
        b["checked"] = int(time.time())
    _save(con, uid, sub)


@bp.route("/confirm", methods=["POST"])
def confirm():
    """Back from Stripe's checkout page: ask Stripe whether it was really paid, so no webhook is needed."""
    if not request.is_json:
        return _err("Please send JSON.", 415)
    if not live():
        return _err("Payments aren't switched on yet.", 503)
    cs = str((request.get_json(silent=True) or {}).get("cs", ""))
    if not cs.startswith("cs_") or len(cs) > 300:
        return _err("That checkout wasn't found.", 404)
    with auth.db() as con:
        u = auth._current(con)
        if not u:
            return _err("Please sign in first.", 401)
        try:
            obj = _stripe(f"/checkout/sessions/{cs}", {"expand[]": "subscription"}, get=True)
        except (RuntimeError, requests.RequestException, ValueError) as e:
            return _err(f"Couldn't check the payment: {e}", 502)
        if str(obj.get("client_reference_id")) != str(u["id"]):
            return _err("That checkout belongs to another account.", 403)
        if obj.get("status") != "complete" or obj.get("payment_status") not in ("paid", "no_payment_required"):
            return _err("That payment didn't go through. Nothing was charged.", 402)
        old = _apply_checkout(con, u["id"], obj)
        if isinstance(obj.get("subscription"), dict):
            _apply_sub(con, u["id"], obj["subscription"])
        user = auth._public(con.execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone())
    for sid in old:
        _stripe_cancel(sid)
    return jsonify({"user": user})


RECHECK = 6 * 3600


def refresh(con, u):
    """Every few hours, ask Stripe about this person's subscription (renewed, canceled, card failed).
    Keeps plans right even with no webhook set up. Returns the fresh row."""
    b = billing(u)
    if not (live() and b.get("sub")) or b.get("status") == "canceled":
        return u
    now = int(time.time())
    if now - int(b.get("checked", 0)) < RECHECK and int(b.get("until", 0)) > now:
        return u
    try:
        obj = _stripe(f"/subscriptions/{b['sub']}", get=True)
    except (RuntimeError, requests.RequestException, ValueError):
        return u
    _apply_sub(con, u["id"], obj, deleted=obj.get("status") == "canceled")
    return con.execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone()


@bp.route("/webhook", methods=["POST"])
def webhook():
    secret = _hook_secret()
    payload = request.get_data()
    if not secret or not _verified(payload, request.headers.get("Stripe-Signature"), secret):
        return _err("Bad signature.", 400)
    ev = json.loads(payload)
    kind, obj = ev.get("type", ""), (ev.get("data") or {}).get("object") or {}
    meta = obj.get("metadata") or {}
    uid = meta.get("uid") or obj.get("client_reference_id")
    if not uid or not str(uid).isdigit():
        return jsonify({"ok": True})
    old = []
    with auth.db() as con:
        if kind == "checkout.session.completed" and obj.get("payment_status") in ("paid", "no_payment_required"):
            old = _apply_checkout(con, uid, obj)
        elif kind in ("customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"):
            _apply_sub(con, uid, obj, deleted=kind.endswith("deleted"))
    for sid in old:
        _stripe_cancel(sid)
    return jsonify({"ok": True})
