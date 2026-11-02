"""Kalshi lookup for Parlay Pal (free, no key). Kalshi's market data is public.

Finds the NBA player-prop market that matches a leg (player + stat + line) so the app can
open that exact market. Nothing is guessed: no match = no link and no price.
If something looks wrong, open http://127.0.0.1:8000/api/kalshi/probe and send me what it shows.
"""
import re
import time
import unicodedata

BASE = "https://api.elections.kalshi.com/trade-api/v2"
SERIES = {"pts": "KXNBAPTS", "reb": "KXNBAREB", "ast": "KXNBAAST", "tpm": "KXNBA3PT"}
TTL = 3 * 60
_cache = {}


def norm(s):
    s = unicodedata.normalize("NFD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"[’'.]", "", s).replace("-", " ")
    s = re.sub(r"\b(jr|sr|ii|iii|iv)\b", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _get(path, params):
    import requests
    return requests.get(BASE + path, params=params, timeout=15)


def markets(series):
    hit = _cache.get(series)
    if hit and time.time() - hit[0] < TTL:
        return hit[1]
    out, cursor = [], None
    for _ in range(6):
        p = {"series_ticker": series, "status": "open", "limit": 1000}
        if cursor:
            p["cursor"] = cursor
        r = _get("/markets", p)
        if r.status_code != 200:
            raise RuntimeError(f"kalshi said {r.status_code}: {r.text[:120]}")
        j = r.json()
        out += j.get("markets", [])
        cursor = j.get("cursor")
        if not cursor:
            break
    _cache[series] = (time.time(), out)
    return out


def _cents(m, side):
    for k in (f"{side}_ask", f"{side}_ask_dollars"):
        v = m.get(k)
        if v is None:
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        return round(v * 100) if k.endswith("dollars") or v <= 1 else round(v)
    return None


def _threshold(m):
    txt = " ".join(str(m.get(k, "")) for k in ("yes_sub_title", "title", "subtitle"))
    mm = re.search(r"(\d+(?:\.\d+)?)\s*\+", txt)
    if mm:
        return float(mm.group(1))
    mm = re.search(r"over\s+(\d+(?:\.\d+)?)", txt, re.I)
    if mm:
        return float(mm.group(1)) + 0.5
    return None


def url_for(m):
    ev = str(m.get("event_ticker", "")).lower()
    ser = ev.split("-")[0] if ev else ""
    return f"https://kalshi.com/markets/{ser}/{ev}" if ev else "https://kalshi.com"


def match(legs):
    out = []
    for l in legs:
        res = {"found": False}
        ser = SERIES.get(l.get("s"))
        if ser and l.get("n") and l.get("bar"):
            who = norm(l["n"])
            want = float(l["bar"])
            for m in markets(ser):
                txt = norm(" ".join(str(m.get(k, "")) for k in ("yes_sub_title", "title", "subtitle")))
                th = _threshold(m)
                if who in txt and th is not None and (abs(th - want) < 1e-6 or abs(th - (want - 0.5)) < 1e-6):
                    ask = _cents(m, "yes")
                    res = {"found": True, "ticker": m.get("ticker"), "url": url_for(m), "yesCents": ask,
                           "american": _american(ask)}
                    break
        out.append(res)
    return out


def _american(cents):
    if not cents or cents <= 0 or cents >= 100:
        return None
    p = cents / 100.0
    return int(round(-100 * p / (1 - p))) if p >= .5 else int(round(100 * (1 - p) / p))


def probe():
    info = {}
    for k, ser in SERIES.items():
        try:
            ms = markets(ser)
            info[k] = {"series": ser, "open_markets": len(ms), "examples": [m.get("title") or m.get("yes_sub_title") for m in ms[:4]]}
        except Exception as e:
            info[k] = {"series": ser, "error": f"{type(e).__name__}: {e}"}
    return info


def lines(games):
    """Which lines does Kalshi list for today's games? Free: Kalshi's market data needs no key and no credits.
    Returns the same shape the site expects from /api/lines."""
    allm = {k: markets(ser) for k, ser in SERIES.items()}
    players, posted = {}, {}
    for g in (games or [])[:16]:
        a, h = str(g.get("a", "")).upper(), str(g.get("h", "")).upper()
        found = False
        for st, ms in allm.items():
            for m in ms:
                ev = str(m.get("event_ticker", "")).upper()
                tail = ev.split("-", 1)[1] if "-" in ev else ev
                if not (a and h and a in tail and h in tail):
                    continue
                th = _threshold(m)
                label = str(m.get("yes_sub_title") or m.get("title") or "")
                who = norm(label.split(":")[0])
                if th is None or not who:
                    continue
                found = True
                players.setdefault(f"{who}|{st}", set()).add(int(round(th)))
        posted[a] = posted[h] = found
    return {"configured": True, "source": "Kalshi", "updated": time.strftime("%H:%M"),
            "players": {k: sorted(v) for k, v in players.items()}, "posted": posted, "creditsLeft": None}
