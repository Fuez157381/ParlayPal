"""Saves a copy of the NBA data so the online site still works if the host cannot reach the NBA.
Run on your PC:  python make_snapshot.py   then upload the snapshots folder to GitHub."""
import json, os
import server

out = os.path.join(server.HERE, "snapshots")
os.makedirs(out, exist_ok=True)
first = server.build(None)
dates = [d for d in first.get("dates", []) if d >= first["stats"]["date"]][:5] or [first["stats"]["date"]]
with open(os.path.join(out, "latest.json"), "w", encoding="utf-8") as f:
    json.dump(first, f)
for d in dates:
    try:
        j = first if d == first["stats"]["date"] else server.build(d)
        with open(os.path.join(out, f"{d}.json"), "w", encoding="utf-8") as f:
            json.dump(j, f)
        print("saved", d)
    except Exception as e:
        print("skipped", d, type(e).__name__, e)
