"""Saves a copy of the NBA data so the online site still works if the host cannot reach the NBA.
Run on your PC:  python make_snapshot.py   then upload the snapshots folder to GitHub.
(GitHub runs this every day: .github/workflows/refresh-data.yml.)"""
import json, os
import server

out = os.path.join(server.HERE, "snapshots")
os.makedirs(out, exist_ok=True)
first = server.build(None)
today = first["stats"]["date"]
dates = [d for d in first.get("dates", []) if d >= today][:5]
if today not in dates:  # a day with no games still gets its own file, so the site shows "no games" instead of searching
    dates.insert(0, today)
with open(os.path.join(out, "latest.json"), "w", encoding="utf-8") as f:
    json.dump(first, f)
for d in dates:
    try:
        j = first if d == today else server.build(d)
        with open(os.path.join(out, f"{d}.json"), "w", encoding="utf-8") as f:
            json.dump(j, f)
        print("saved", d)
    except Exception as e:
        print("skipped", d, type(e).__name__, e)
# every player in the league (search shows anyone, even when his team is off)
try:
    allp = server.build(None, all_teams=True)
    with open(os.path.join(out, "players.json"), "w", encoding="utf-8") as f:
        json.dump({"players": allp["players"], "updated": allp["stats"]["updated"]}, f, separators=(",", ":"))
    print("saved players.json", len(allp["players"]), "players")
except Exception as e:
    print("skipped players.json", type(e).__name__, e)
# old days are never asked for again
keep = set(dates) | {"latest"}
for name in os.listdir(out):
    if name.endswith(".json") and name[:-5] not in keep and name[:-5] < today:
        os.remove(os.path.join(out, name))
        print("removed old", name)
