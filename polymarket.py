"""Polymarket public market data (free, no key). Probe only for now: shows what NBA markets exist
so player-prop matching can be built on real titles instead of guesses."""
import requests

GAMMA = "https://gamma-api.polymarket.com"


def probe():
    out = {}
    for label, params in (("nba_events", {"tag_slug": "nba", "closed": "false", "limit": 60}),):
        r = requests.get(GAMMA + "/events", params=params, timeout=20, headers={"User-Agent": "parlaypal"})
        r.raise_for_status()
        evs = r.json()
        rows = []
        for e in evs[:60]:
            ms = e.get("markets") or []
            rows.append({"event": e.get("title"), "slug": e.get("slug"), "markets": len(ms),
                         "sample": [m.get("question") for m in ms[:3]]})
        out[label] = {"count": len(evs), "events": rows}
    props = [r for r in out["nba_events"]["events"] if any(w in str(r["event"]).lower() + str(r["sample"]).lower()
                                                           for w in ("points", "rebounds", "assists", "3-point", "threes"))]
    out["player_prop_like"] = props[:15]
    return out
