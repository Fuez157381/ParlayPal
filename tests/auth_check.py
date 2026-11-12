"""Runs the account system end to end with Flask's test client: sign up, sign in, spins, locker, profile,
password change, sign out, delete. Uses whatever database auth.py picks (DATABASE_URL Postgres, or AUTH_DB sqlite).
Prints PASS/FAIL lines and exits 1 on any failure."""
import os, sys, json
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import server  # noqa: E402

ok = True
def check(name, cond, extra=""):
    global ok
    print(("PASS " if cond else "FAIL ") + name + (" " + str(extra) if extra and not cond else ""))
    ok = ok and bool(cond)

c = server.app.test_client()
email = "check+%d@example.com" % os.getpid()
r = c.post("/api/auth/signup", json={"name": "Test Player", "email": email, "password": "correct horse"})
j = r.get_json(); check("sign up", r.status_code == 200 and j["user"]["email"] == email, (r.status_code, j))
lk = j["user"]["locker"]; check("new account gets starter spins and the basketball", lk["spins"] == 3 and lk["owned"] == {"ball": 1}, lk)
check("signed in after sign up", c.get("/api/auth/me").get_json()["user"]["email"] == email)
r = c.post("/api/auth/signup", json={"name": "X", "email": email, "password": "correct horse"}); check("same email twice is refused", r.status_code == 409)
got = []
for i in range(3):
    r = c.post("/api/auth/spin", json={}); j = r.get_json(); check("spin %d" % (i + 1), r.status_code == 200 and j["got"] in ("baseball", "chip", "dice", "eight", "goat", "coin", "dad"), (r.status_code, j)); got.append(j.get("got"))
r = c.post("/api/auth/spin", json={}); check("4th spin refused (out of spins)", r.status_code == 409, r.get_json())
u = c.get("/api/auth/me").get_json()["user"]; check("locker remembers the rolls", sum(u["locker"]["owned"].values()) == 4 and u["locker"]["spins"] == 0, u["locker"])
r = c.post("/api/auth/mascot", json={"mascot": got[0]}); check("equip a rolled Pal", r.status_code == 200 and r.get_json()["user"]["mascot"] == got[0], r.get_json())
locked = [k for k in ("baseball", "chip", "dice", "eight", "goat", "coin", "dad") if k not in u["locker"]["owned"]][0]
r = c.post("/api/auth/mascot", json={"mascot": locked}); check("can't equip a Pal you don't own", r.status_code == 403)
r = c.post("/api/auth/profile", json={"name": "Renamed", "email": email, "team": "BOS", "color": "#A78BFA"}); check("profile save", r.status_code == 200 and r.get_json()["user"]["team"] == "BOS")
r = c.post("/api/auth/parlays", json={"trk": [{"id": 111, "legs": []}], "saved": [{"ts": 222, "name": "Mine", "legs": []}], "gone": []}); j = r.get_json(); check("parlays saved to the account", r.status_code == 200 and [t["id"] for t in j["trk"]] == [111] and j["saved"][0]["name"] == "Mine", j)
r = c.post("/api/auth/parlays", json={}); check("another device gets them back", [t["id"] for t in r.get_json()["trk"]] == [111], r.get_json())
r = c.post("/api/auth/parlays", json={"gone": ["t111"]}); check("removing a parlay removes it everywhere", r.get_json()["trk"] == [], r.get_json())
c.post("/api/auth/logout", json={}); check("signed out", c.get("/api/auth/me").get_json()["user"] is None)
r = c.post("/api/auth/login", json={"email": email, "password": "wrong password"}); check("wrong password refused", r.status_code == 401)
r = c.post("/api/auth/login", json={"email": email, "password": "correct horse"}); check("sign in", r.status_code == 200)
r = c.post("/api/auth/password", json={"current": "correct horse", "new": "battery staple"}); check("change password", r.status_code == 200)
c.post("/api/auth/logout", json={})
check("new password works", c.post("/api/auth/login", json={"email": email, "password": "battery staple"}).status_code == 200)
r = c.post("/api/auth/delete", json={"password": "battery staple"}); check("delete account", r.status_code == 200)
check("deleted account can't sign in", c.post("/api/auth/login", json={"email": email, "password": "battery staple"}).status_code == 401)
print("ALL PASSED" if ok else "SOME CHECKS FAILED")
sys.exit(0 if ok else 1)
