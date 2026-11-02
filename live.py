"""Live and final box scores for tracked parlays (NBA's own live data, free)."""
import time

_cache = {}


def box(gid):
    now = time.time()
    c = _cache.get(gid)
    if c and now - c[0] < (300 if c[1].get("status") == 3 else 20):
        return c[1]
    from nba_api.live.nba.endpoints import boxscore
    g = boxscore.BoxScore(game_id=gid, timeout=20).get_dict()["game"]
    out = {"status": g.get("gameStatus"), "text": str(g.get("gameStatusText", "")).strip(), "players": {}}
    for side in ("homeTeam", "awayTeam"):
        for p in g.get(side, {}).get("players", []):
            s = p.get("statistics", {}) or {}
            out["players"][str(p.get("personId"))] = {
                "pts": s.get("points", 0), "reb": s.get("reboundsTotal", 0), "ast": s.get("assists", 0),
                "tpm": s.get("threePointersMade", 0), "min": s.get("minutes", ""), "played": str(p.get("played", "")) == "1"}
    _cache[gid] = (now, out)
    return out


def many(gids):
    res = {}
    for gid in list(dict.fromkeys(gids))[:12]:
        try:
            res[gid] = box(str(gid))
        except Exception as e:
            res[gid] = {"error": f"{type(e).__name__}: {e}"}
    return res
