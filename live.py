"""Live and final box scores for tracked parlays, from ESPN (free, and it works from cloud servers)."""
import time

_cache = {}


def box(gid):
    now = time.time()
    c = _cache.get(gid)
    if c and now - c[0] < (300 if c[1].get("status") == 3 else 20):
        return c[1]
    if str(gid).startswith(("ten:", "ufc:", "esp:")):
        import matchsports
        out = matchsports.box(gid)
    elif str(gid).startswith(("nfl:", "nhl:", "soc:")):
        out = __import__({"soc": "soccer"}.get(str(gid).split(":")[0], str(gid).split(":")[0])).box(gid)
    else:
        import espn
        out = espn.box(gid)
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
