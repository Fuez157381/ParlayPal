"""Plans with a pretend Stripe: checkout, coming back from checkout, the manage page, and a canceled plan.
Needs only STRIPE_SECRET_KEY on the real site; no webhook. Prints PASS/FAIL lines and exits 1 on any failure."""
import json, os, sys, tempfile, time
os.environ["AUTH_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["STRIPE_SECRET_KEY"] = "sk_live_fake"
os.environ.pop("STRIPE_WEBHOOK_SECRET", None)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import server, pay, auth  # noqa

calls = []
STATE = {"sub_status": "active", "portal_cfg": False}
end = int(time.time()) + 30 * 86400


def fake(path, data=None, get=False):
    calls.append((path, get, dict(data or {})))
    if path == "/checkout/sessions":
        return {"id": "cs_1", "url": "https://checkout.stripe.com/c/pay/cs_1"}
    if path.startswith("/checkout/sessions/"):
        cs = path.rsplit("/", 1)[1]
        uid = STATE["uid"] if cs == "cs_1" else 999
        return {"id": cs, "status": "complete", "payment_status": "paid", "customer": "cus_1", "client_reference_id": str(uid),
                "metadata": {"uid": str(uid), "item": "pro"},
                "subscription": {"id": "sub_pro", "customer": "cus_1", "status": "active", "current_period_end": end, "metadata": {"uid": str(uid), "item": "pro"}}}
    if path.startswith("/subscriptions/"):
        return {"id": "sub_pro", "customer": "cus_1", "status": STATE["sub_status"], "current_period_end": end, "metadata": {"item": "pro"}}
    if path == "/billing_portal/sessions":
        if not STATE["portal_cfg"] and "configuration" not in (data or {}):
            raise RuntimeError("No configuration provided and your live mode default configuration has not been created.")
        return {"url": "https://billing.stripe.com/p/session/x"}
    if path == "/billing_portal/configurations":
        STATE["portal_cfg"] = True
        return {"id": "bpc_1"}
    raise AssertionError(path)


pay._stripe = fake
ok = True


def check(name, cond, extra=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else " " + str(extra)))
    ok = ok and bool(cond)


c = server.app.test_client()
check("payments count as on with only the secret key", c.get("/api/pay/config").get_json()["live"] is True)
u = c.post("/api/auth/signup", json={"name": "K", "email": "k%d@example.test" % os.getpid(), "password": "correct horse"}).get_json()["user"]
STATE["uid"] = u["id"]
r = c.post("/api/pay/checkout", json={"item": "pro"}); j = r.get_json()
sent = [x for x in calls if x[0] == "/checkout/sessions"][-1][2]
check("checkout opens Stripe for $12.99 a month", r.status_code == 200 and j["url"].startswith("https://checkout.stripe.com") and sent["line_items[0][price_data][unit_amount]"] == 1299 and sent["line_items[0][price_data][recurring][interval]"] == "month", (r.status_code, j, sent))
check("Stripe sends people back with the checkout id", "cs={CHECKOUT_SESSION_ID}" in sent["success_url"], sent["success_url"])
r = c.post("/api/pay/checkout", json={"item": "plus"}); j = r.get_json(); s2 = [x for x in calls if x[0] == "/checkout/sessions"][-1][2]
check("Plus checkout is $7.99 a month", s2["line_items[0][price_data][unit_amount]"] == 799)
r = c.post("/api/pay/confirm", json={"cs": "cs_1"}); j = r.get_json()
check("coming back from checkout turns on Pro, no webhook needed", r.status_code == 200 and j["user"]["plan"] == "pro" and j["user"]["credits"]["limit"] is None, j)
spins = j["user"]["locker"]["spins"]
r = c.post("/api/pay/confirm", json={"cs": "cs_1"})
check("confirming twice doesn't give bonus spins twice", r.get_json()["user"]["locker"]["spins"] == spins)
r = c.post("/api/pay/confirm", json={"cs": "cs_other"})
check("someone else's checkout is refused", r.status_code == 403)
check("Pro shows on the account", c.get("/api/auth/me").get_json()["user"]["plan"] == "pro")
r = c.post("/api/pay/portal", json={}); j = r.get_json()
check("Manage opens Stripe's page, setting it up the first time", r.status_code == 200 and "billing.stripe.com" in j["url"] and STATE["portal_cfg"], j)
# a few hours later the person canceled on Stripe
with auth.db() as con:
    row = con.execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone()
    b = json.loads(row["billing"]); b["checked"] = 0
    con.execute("UPDATE users SET billing=? WHERE id=?", (json.dumps(b), u["id"])); con.commit()
STATE["sub_status"] = "canceled"
check("a canceled plan is noticed without a webhook", c.get("/api/auth/me").get_json()["user"]["plan"] == "free")
print("ALL PASSED" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
