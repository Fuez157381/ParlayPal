"""Payments with Stripe: Parlay Pal Pro ($9.99 a month) and spin packs.

Switched on by two Vercel settings (env vars):
  STRIPE_SECRET_KEY      sk_test_... while testing, sk_live_... for real money
  STRIPE_WEBHOOK_SECRET  whsec_... from the webhook Stripe sends to /api/pay/webhook
Without them every route answers "payments aren't switched on yet" and nothing is charged.

Stripe's hosted checkout page takes the card, so card numbers never touch this site.
What someone bought is only trusted when Stripe's signed webhook says it was paid."""
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

ITEMS = {
    "pro": {"name": "Parlay Pal Pro", "cents": 999, "monthly": True},
    "spins10": {"name": "10 spins", "cents": 50, "spins": 10},
    "spins25": {"name": "25 spins", "cents": 100, "spins": 25},
}


def _key():
    return os.environ.get("STRIPE_SECRET_KEY", "").strip()


def _hook_secret():
    return os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()


def live():
    return bool(_key() and _hook_secret())


def _site():
    s = os.environ.get("SITE_URL", "").strip().rstrip("/")
    return s or request.host_url.rstrip("/")


def _stripe(path, data):
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
        if it.get("monthly") and is_pro(u):
            return _err("You already have Parlay Pal Pro.", 409)
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
            "success_url": f"{site}/?paid={item}#home",
            "cancel_url": f"{site}/?paid=no#home",
        }
        if it.get("monthly"):
            data["line_items[0][price_data][recurring][interval]"] = "month"
            data["subscription_data[metadata][uid]"] = str(u["id"])
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
        try:
            s = _stripe("/billing_portal/sessions", {"customer": cus, "return_url": _site() + "/#home"})
        except (RuntimeError, requests.RequestException, ValueError) as e:
            return _err(f"Couldn't open billing: {e}", 502)
        return jsonify({"url": s["url"]})


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


def _save(con, uid, fn):
    u = con.execute("SELECT * FROM users WHERE id=?", (int(uid),)).fetchone()
    if not u:
        return
    b = billing(u)
    lk = auth._locker(u)
    fn(b, lk)
    con.execute("UPDATE users SET billing=?, locker=? WHERE id=?", (json.dumps(b), json.dumps(lk), u["id"]))
    con.commit()


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
    with auth.db() as con:
        if kind == "checkout.session.completed" and obj.get("payment_status") in ("paid", "no_payment_required"):
            item = ITEMS.get(meta.get("item", ""), {})

            def paid(b, lk):
                seen = b.get("seen") or []
                if obj.get("id") in seen:      # Stripe can send the same event twice
                    return
                b["seen"] = (seen + [obj.get("id")])[-40:]
                if obj.get("customer"):
                    b["customer"] = obj["customer"]
                if item.get("spins"):
                    lk["spins"] = int(lk.get("spins", 0)) + item["spins"]
                if item.get("monthly"):
                    b.update(status="active", sub=obj.get("subscription"), until=int(time.time()) + 32 * 86400)
            _save(con, uid, paid)
        elif kind in ("customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"):
            def sub(b, lk):
                b["sub"] = obj.get("id")
                if obj.get("customer"):
                    b["customer"] = obj["customer"]
                b["status"] = "canceled" if kind.endswith("deleted") else obj.get("status", "")
                end = obj.get("current_period_end") or ((obj.get("items") or {}).get("data") or [{}])[0].get("current_period_end")
                if end:
                    b["until"] = int(end)
                b["cancelAtEnd"] = bool(obj.get("cancel_at_period_end"))
            _save(con, uid, sub)
    return jsonify({"ok": True})
