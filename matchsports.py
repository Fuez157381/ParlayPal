"""Head-to-head sports for Parlay Pal: tennis, UFC and esports (CS2, League of Legends, Valorant).

The bet in these sports is "X wins". Kalshi (free, public) lists every match you can actually bet, with
a live price. The research comes from real results:
  tennis:  ESPN match history (wins, games won per match, straight-set wins, recent form)
  UFC:     ESPN fight history (wins, finishes, method, round), record, reach, age
  esports: Kalshi's own settled markets (every past match and who won it)
Same shape as the NBA's /api/data. A "player" here is a competitor: the tennis player, the fighter or
the esports team. Keys: tennis "tn<espn id>", UFC "uf<espn id>", esports "es<team slug>".
Stats: tw / uw / ew = to win (yes/no). Research-only stats (no line): tgw (games won), tss (straight-set
win), ufin (won by finish).
"""
import datetime as dt
import re
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

import requests

ET = ZoneInfo("America/New_York")
KAL = "https://api.elections.kalshi.com/trade-api/v2"
CORE = "http://sports.core.api.espn.com/v2/sports"
SEARCH = "https://site.web.api.espn.com/apis/search/v2"
SITE = "https://site.api.espn.com/apis/site/v2/sports"
WEB = "https://site.web.api.espn.com/apis/common/v3/sports"
HIST = 12
TTL_SCHED, TTL_HIST = 20 * 60, 12 * 3600
SERIES = {"ten": ("KXATPMATCH", "KXWTAMATCH"), "ufc": ("KXUFCFIGHT",),
          "esp": ("KXCS2GAME", "KXLOLGAME", "KXVALORANTGAME")}
GAME_NAME = {"KXCS2GAME": "Counter-Strike 2", "KXLOLGAME": "League of Legends", "KXVALORANTGAME": "Valorant",
             "KXATPMATCH": "ATP", "KXWTAMATCH": "WTA", "KXUFCFIGHT": "UFC"}
WIN = {"ten": "tw", "ufc": "uw", "esp": "ew"}
MON = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


def _get(url, params=None, timeout=20):
    r = requests.get(url, params=params, timeout=timeout, headers={"User-Agent": "Mozilla/5.0 ParlayPal"})
    r.raise_for_status()
    return r.json()


def slug(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def norm(s):
    import unicodedata
    s = unicodedata.normalize("NFD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", s)).strip()


# ---------- Kalshi ----------
def kalshi_markets(series, status="open", pages=3):
    out, cursor = [], None
    for _ in range(pages):
        p = {"series_ticker": series, "status": status, "limit": 1000}
        if cursor:
            p["cursor"] = cursor
        j = _get(KAL + "/markets", p)
        out += j.get("markets") or []
        cursor = j.get("cursor")
        if not cursor:
            break
    return out


def _cents(m):
    for k in ("yes_ask_dollars", "yes_ask", "last_price_dollars", "last_price"):
        v = m.get(k)
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        if v > 0:
            return round(v * 100) if (k.endswith("dollars") or v <= 1) else round(v)
    return None


def event_time(ticker, fallback=None):
    """KXCS2GAME-26NOV101430OLDYTIGERES -> 2026-11-10 14:30 Eastern (UFC/tennis tickers have no time)."""
    m = re.match(r"^[A-Z0-9]+-(\d{2})([A-Z]{3})(\d{2})(\d{4})?", ticker or "")
    if not m:
        return fallback
    y, mon, d, hm = int(m.group(1)), MON.get(m.group(2), 1), int(m.group(3)), m.group(4)
    h, mi = (int(hm[:2]), int(hm[2:])) if hm else (12, 0)
    try:
        return dt.datetime(2000 + y, mon, d, h, mi, tzinfo=ET)
    except ValueError:
        return fallback


def kalshi_matches(sport):
    """Upcoming matches: [{ev, series, when, sides:[{name, cents, ticker}]}] from Kalshi's open markets."""
    evs = {}
    for ser in SERIES[sport]:
        try:
            ms = kalshi_markets(ser)
        except Exception:
            continue
        for m in ms:
            name = (m.get("yes_sub_title") or "").strip()
            if not name:
                continue
            e = evs.setdefault(m["event_ticker"], {"ev": m["event_ticker"], "series": ser, "sides": [],
                                                   "when": (event_time(m["event_ticker"]) or dt.datetime.now(ET)).isoformat()})
            e["sides"].append({"name": name, "cents": _cents(m), "ticker": m.get("ticker")})
    return [e for e in evs.values() if len(e["sides"]) == 2]


# ---------- tennis (ESPN) ----------
def espn_athlete_id(name, sport):
    """ESPN athlete id for a player name in tennis or MMA."""
    try:
        j = _get(SEARCH, {"query": name, "limit": 6})
    except Exception:
        return None
    want = norm(name)
    best = None
    for r in j.get("results") or []:
        if r.get("type") != "player":
            continue
        for c in r.get("contents") or []:
            if c.get("sport") != sport:
                continue
            m = re.search(r"a:(\d+)", c.get("uid") or "")
            if not m:
                continue
            if norm(c.get("displayName")) == want:
                return m.group(1)
            best = best or m.group(1)
    return best


def tennis_history(aid):
    """Last matches, newest first: {date, opp, oppName, win, gw, gl, sets_w, sets_l, tour}."""
    j = _get(f"{CORE}/tennis/athletes/{aid}/eventlog", {"limit": 40})
    items = [i for i in ((j.get("events") or {}).get("items") or []) if i.get("played")]
    rows = []
    for it in items[-HIST - 4:][::-1]:
        try:
            c = _get(it["competition"]["$ref"])
        except Exception:
            continue
        if "single" not in str((c.get("type") or {}).get("text", "")).lower():
            continue
        comps = c.get("competitors") or []
        me = next((x for x in comps if str(x.get("id")) == str(aid)), None)
        op = next((x for x in comps if str(x.get("id")) != str(aid)), None)
        if not me or not op:
            continue
        try:
            ls_me = _get(me["linescores"]["$ref"]).get("items") or []
            ls_op = _get(op["linescores"]["$ref"]).get("items") or []
        except Exception:
            ls_me, ls_op = [], []
        gw = sum(float(x.get("value") or 0) for x in ls_me)
        gl = sum(float(x.get("value") or 0) for x in ls_op)
        sw = sum(1 for a, b in zip(ls_me, ls_op) if float(a.get("value") or 0) > float(b.get("value") or 0))
        sl = sum(1 for a, b in zip(ls_me, ls_op) if float(a.get("value") or 0) < float(b.get("value") or 0))
        oid = str(op.get("id"))
        oname = ""
        try:
            oname = _get(op["athlete"]["$ref"]).get("displayName") or ""
        except Exception:
            pass
        rows.append({"date": dt.datetime.fromisoformat(c["date"].replace("Z", "+00:00")).astimezone(ET).strftime("%Y-%m-%d"),
                     "opp": oid, "oppName": oname, "win": 1 if me.get("winner") else 0,
                     "gw": gw, "gl": gl, "sw": sw, "sl": sl})
        if len(rows) >= HIST:
            break
    return rows


# ---------- UFC (ESPN) ----------
def ufc_card():
    """The next UFC card from ESPN: [{id, aid, an, bid, bn, cls, when, order}]"""
    j = _get(SITE + "/mma/ufc/scoreboard")
    out = []
    for e in j.get("events") or []:
        for c in e.get("competitions") or []:
            cs = c.get("competitors") or []
            if len(cs) != 2:
                continue
            st = ((c.get("status") or {}).get("type") or {}).get("state")
            if st == "post":
                continue
            out.append({"id": str(c.get("id")), "event": e.get("name"), "when": c.get("date") or e.get("date"),
                        "cls": (c.get("type") or {}).get("abbreviation") or "",
                        "sides": [{"id": str(x.get("id")), "name": (x.get("athlete") or {}).get("displayName"),
                                   "rec": ((x.get("records") or [{}])[0]).get("summary")} for x in cs]})
    return out


def ufc_profile(aid):
    j = _get(f"{WEB}/mma/athletes/{aid}")
    a = j.get("athlete") or {}
    st = {s.get("name"): s.get("displayValue") for s in ((a.get("statsSummary") or {}).get("statistics") or [])}
    return {"reach": a.get("displayReach"), "height": a.get("displayHeight"), "age": a.get("age"),
            "stance": (a.get("stance") or {}).get("text") if isinstance(a.get("stance"), dict) else a.get("stance"),
            "wld": st.get("wins-losses-draws"), "ko": st.get("tkos-tkoLosses"), "sub": st.get("submissions-submissionLosses")}


def ufc_history(aid):
    j = _get(f"{CORE}/mma/athletes/{aid}/eventlog", {"limit": 40})
    items = [i for i in ((j.get("events") or {}).get("items") or []) if i.get("played")]
    rows = []
    for it in items[:HIST + 4]:
        try:
            c = _get(it["competition"]["$ref"])
            s = _get(c["status"]["$ref"])
        except Exception:
            continue
        comps = c.get("competitors") or []
        me = next((x for x in comps if str(x.get("id")) == str(aid)), None)
        op = next((x for x in comps if str(x.get("id")) != str(aid)), None)
        if not me or not op or (s.get("type") or {}).get("state") != "post":
            continue
        res = (s.get("result") or {})
        method = res.get("shortDisplayName") or res.get("displayName") or ""
        win = 1 if me.get("winner") else 0
        evm = re.search(r"events/(\d+)", (it.get("event") or {}).get("$ref", ""))
        per = int(s.get("period") or 1)
        clock = float(s.get("clock") or 0)
        rows.append({"date": dt.datetime.fromisoformat(c["date"].replace("Z", "+00:00")).astimezone(ET).strftime("%Y-%m-%d"),
                     "ev": evm.group(1) if evm else "", "mins": round((per - 1) * 5 + (clock / 60 if clock else 5), 2),
                     "opp": str(op.get("id")), "win": win, "method": method, "round": s.get("period"),
                     "fin": 1 if win and not re.search(r"dec", method, re.I) else 0,
                     "lostfin": 1 if not win and method and not re.search(r"dec", method, re.I) else 0})
        if len(rows) >= HIST:
            break
    rows.sort(key=lambda r: r["date"], reverse=True)
    return rows


def ufc_stats(aid):
    """Per-fight numbers by ESPN event id: significant strikes, total strikes, knockdowns, takedowns."""
    j = _get(f"{WEB}/mma/athletes/{aid}/stats")
    out = {}
    for cat in j.get("categories") or []:
        labels = [str(x).strip() for x in cat.get("labels") or []]
        ix = {k: i for i, k in enumerate(labels)}
        for e in cat.get("statistics") or []:
            st = e.get("stats") or []
            row = out.setdefault(str(e.get("eventId")), {})

            def num(k):
                try:
                    return float(str(st[ix[k]]).split("/")[0])
                except (KeyError, IndexError, ValueError):
                    return None
            for k, name in (("SSL", "ssl"), ("SSA", "ssa"), ("TSL", "tsl"), ("KD", "kd"), ("TDL", "tdl"), ("TDA", "tda")):
                if k in ix:
                    row[name] = num(k)
    return out


def _inches(v):
    m = re.search(r"([\d.]+)", str(v or ""))
    return float(m.group(1)) if m else None


def ufc_case(me, op, mr, orr, mp, opp_prof):
    """Why each fighter can win, from the numbers. Returns (pros, cons)."""
    pros, cons = [], []
    def avg(rows, k):
        v = [r[k] for r in rows if r.get(k) is not None]
        return sum(v) / len(v) if v else None
    def pace(rows):
        v = [(r["ssl"], r["mins"]) for r in rows if r.get("ssl") is not None and r.get("mins")]
        return sum(a for a, _ in v) / max(0.1, sum(b for _, b in v)) if v else None
    a_ssl, b_ssl = avg(mr, "ssl"), avg(orr, "ssl")
    a_pace, b_pace = pace(mr), pace(orr)
    a_td, b_td = avg(mr, "tdl"), avg(orr, "tdl")
    a_kd = sum(r.get("kd") or 0 for r in mr)
    if a_pace and b_pace:
        if a_pace >= b_pace * 1.2:
            pros.append(f"Out-works the opponent on the feet: {a_pace:.1f} significant strikes a minute to {b_pace:.1f}.")
        elif b_pace >= a_pace * 1.2:
            cons.append(f"Slower pace: {a_pace:.1f} significant strikes a minute to {b_pace:.1f}.")
    elif a_ssl is not None and b_ssl is not None and a_ssl > b_ssl * 1.2:
        pros.append(f"Lands more: {a_ssl:.0f} significant strikes a fight to {b_ssl:.0f}.")
    if a_td is not None and a_td >= 1 and (b_td is None or a_td > b_td + 0.7):
        pros.append(f"Wrestling edge: {a_td:.1f} takedowns a fight" + (f" to {b_td:.1f}." if b_td is not None else "."))
    if a_kd >= 2:
        pros.append(f"Power: {int(a_kd)} knockdowns in these fights.")
    fins = sum(r["fin"] for r in mr)
    if fins >= 3 and sum(r["lostfin"] for r in orr) >= 2:
        pros.append(f"Finisher against a fighter who has been stopped {sum(r['lostfin'] for r in orr)} times recently.")
    r1, r2 = _inches((mp or {}).get("reach")), _inches((opp_prof or {}).get("reach"))
    if r1 and r2 and abs(r1 - r2) >= 3:
        (pros if r1 > r2 else cons).append(f"{'Longer' if r1 > r2 else 'Shorter'} reach: {r1:g} in to {r2:g} in.")
    a1, a2 = (mp or {}).get("age"), (opp_prof or {}).get("age")
    if a1 and a2 and abs(a1 - a2) >= 5:
        (pros if a1 < a2 else cons).append(f"{'Younger' if a1 < a2 else 'Older'}: {a1} to {a2}.")
    f1, f2 = sum(r["win"] for r in mr[:5]), sum(r["win"] for r in orr[:5])
    if f1 >= f2 + 2:
        pros.append(f"Better form: {f1}-{min(5, len(mr)) - f1} in the last five to {f2}-{min(5, len(orr)) - f2}.")
    elif f2 >= f1 + 2:
        cons.append(f"Worse form: {f1}-{min(5, len(mr)) - f1} in the last five to {f2}-{min(5, len(orr)) - f2}.")
    if mr:
        days = (dt.datetime.now(ET).date() - dt.date.fromisoformat(mr[0]["date"])).days
        if days > 400:
            cons.append(f"Long layoff: {days // 30} months since the last fight.")
    lf = sum(r["lostfin"] for r in mr)
    if lf >= 2:
        cons.append(f"Durability: stopped {lf} times in these fights.")
    return pros, cons


# ---------- esports (Kalshi results) ----------
def esports_history(series_list):
    """{team slug: [{date, win, opp}]} from every settled Kalshi esports match."""
    out = {}
    for ser in series_list:
        try:
            ms = kalshi_markets(ser, "settled", pages=3)
        except Exception:
            continue
        by_ev = {}
        for m in ms:
            if m.get("result") in ("yes", "no") and m.get("yes_sub_title"):
                by_ev.setdefault(m["event_ticker"], []).append(m)
        for ev, pair in by_ev.items():
            if len(pair) != 2 or sorted(m["result"] for m in pair) != ["no", "yes"]:     # skip cancelled or voided matches
                continue
            when = event_time(ev) or dt.datetime.now(ET)
            for m, o in ((pair[0], pair[1]), (pair[1], pair[0])):
                out.setdefault(slug(m["yes_sub_title"]), []).append(
                    {"date": when.strftime("%Y-%m-%d"), "win": 1 if m["result"] == "yes" else 0,
                     "opp": slug(o["yes_sub_title"]), "oppName": o["yes_sub_title"], "game": GAME_NAME.get(ser, "")})
    for k in out:
        out[k].sort(key=lambda r: r["date"], reverse=True)
    return out


# ---------- build ----------
def _label(when):
    return f"{when.hour % 12 or 12}:{when:%M} {'AM' if when.hour < 12 else 'PM'}"


def build(sport, date_str, cached):
    today = dt.datetime.now(ET).strftime("%Y-%m-%d")
    win = WIN[sport]
    # upcoming matches
    if sport == "ufc":
        card, src = cached("ufc:card", TTL_SCHED, ufc_card)
        prices = {}
        for e in cached("ufc:kalshi", TTL_SCHED, lambda: kalshi_matches("ufc"))[0]:
            for sd in e["sides"]:
                prices[norm(sd["name"])] = sd["cents"]
        matches = []
        for c in card:
            when = dt.datetime.fromisoformat(c["when"].replace("Z", "+00:00")).astimezone(ET)
            matches.append({"id": "ufc:" + c["id"], "when": when, "league": (c["event"] or "UFC") + (" · " + c["cls"] if c["cls"] else ""),
                            "sides": [{"key": "uf" + s["id"], "aid": s["id"], "name": s["name"], "rec": s.get("rec"),
                                       "cents": prices.get(norm(s["name"]))} for s in c["sides"]]})
    else:
        evs, src = cached(f"{sport}:kalshi", TTL_SCHED, lambda: kalshi_matches(sport))
        matches = []
        for e in evs:
            when = e["when"] if isinstance(e["when"], dt.datetime) else dt.datetime.fromisoformat(str(e["when"]))
            sides = []
            for sd in e["sides"]:
                k = "es" + slug(sd["name"]) if sport == "esp" else None
                sides.append({"key": k, "name": sd["name"], "cents": sd["cents"]})
            matches.append({"id": f"{sport}:{e['ev']}", "when": when, "league": GAME_NAME.get(e["series"], ""), "sides": sides})
    # serialize datetimes from the cache
    for m in matches:
        if isinstance(m["when"], str):
            m["when"] = dt.datetime.fromisoformat(m["when"])
    bydate = {}
    for m in matches:
        bydate.setdefault(m["when"].strftime("%Y-%m-%d"), []).append(m)
    dates = sorted(d for d in bydate if d >= today) or sorted(bydate)
    if not date_str or date_str not in bydate:
        date_str = (dates or [today])[0]
    todays = sorted(bydate.get(date_str, []), key=lambda m: m["when"])

    # tennis: find ESPN ids for the players
    if sport == "ten":
        def find(sd):
            aid, _ = cached(f"ten:id:{norm(sd['name'])}", 30 * 24 * 3600, lambda: espn_athlete_id(sd["name"], "tennis"))
            sd["aid"] = aid
            sd["key"] = "tn" + slug(sd["name"])
        with ThreadPoolExecutor(8) as ex:
            list(ex.map(find, [sd for m in todays for sd in m["sides"]]))

    # histories
    hist = {}
    if sport == "esp":
        allh, _ = cached("esp:history", TTL_HIST, lambda: esports_history(SERIES["esp"]))
        for m in todays:
            for sd in m["sides"]:
                hist[sd["key"]] = allh.get(sd["key"][2:], [])[:HIST]
    else:
        def load(sd):
            if not sd.get("aid"):
                return sd["key"], []
            fn = tennis_history if sport == "ten" else ufc_history
            try:
                r, _ = cached(f"{sport}:hist2:{sd['aid']}", TTL_HIST, lambda: fn(sd["aid"]))
            except Exception:
                r = []
            return sd["key"], r
        with ThreadPoolExecutor(6) as ex:
            hist = dict(ex.map(load, [sd for m in todays for sd in m["sides"]]))
    prof, stats = {}, {}
    if sport == "ufc":
        def ls(sd):
            try:
                return sd["key"], cached(f"ufc:stats:{sd['aid']}", TTL_HIST, lambda: ufc_stats(sd["aid"]))[0]
            except Exception:
                return sd["key"], {}
        with ThreadPoolExecutor(6) as ex:
            stats = dict(ex.map(ls, [sd for m in todays for sd in m["sides"]]))
        for k, st in stats.items():                   # opponents' rows get their numbers too (for the matchup case)
            for r in hist.get(k, []):
                r.update(st.get(r.get("ev")) or {})
        def lp(sd):
            try:
                return sd["key"], cached(f"ufc:prof:{sd['aid']}", TTL_HIST, lambda: ufc_profile(sd["aid"]))[0]
            except Exception:
                return sd["key"], {}
        with ThreadPoolExecutor(6) as ex:
            prof = dict(ex.map(lp, [sd for m in todays for sd in m["sides"]]))

    games, players, teams, net = [], [], {}, {}
    for i, m in enumerate(todays, 1):
        a, h = m["sides"][0], m["sides"][1]
        games.append({"i": i, "id": m["id"], "a": a["key"], "h": h["key"], "d": f"{m['when']:%a %b} {m['when'].day}",
                      "t": _label(m["when"]), "lg": sport, "league": m["league"],
                      "kp": {a["key"]: a.get("cents"), h["key"]: h.get("cents")}})
        for me, op in ((a, h), (h, a)):
            teams[me["key"]] = {"n": me["name"]}
            rows = hist.get(me["key"], [])
            if len(rows) < 2:
                continue
            p = {"id": me.get("aid") or me["key"], "n": me["name"], "t": me["key"], "lg": sport, "ncur": len(rows),
                 "dates": [r["date"] for r in rows], "min": [],
                 "mu": [f"{me['key']} vs. {('tn' + slug(r.get('oppName') or r['opp'])) if sport == 'ten' else ('uf' + str(r['opp'])) if sport == 'ufc' else ('es' + str(r['opp']))}" for r in rows],
                 win: [r["win"] for r in rows], "kp": me.get("cents"), "oppn": op["name"], "info": []}
            wins = sum(r["win"] for r in rows)
            if sport == "ten":
                p["tgw"] = [r["gw"] for r in rows]
                p["tss"] = [1 if r["win"] and r["sl"] == 0 else 0 for r in rows]
                avg_gw = sum(r["gw"] for r in rows) / len(rows)
                p["info"].append(f"Won {wins} of the last {len(rows)} matches, winning {avg_gw:.1f} games per match.")
                h2h = [r for r in rows if str(r["opp"]) == str(op.get("aid"))]
                if h2h:
                    p["info"].append(f"Head to head with {op['name']} in these matches: {sum(r['win'] for r in h2h)}-{len(h2h) - sum(r['win'] for r in h2h)}.")
            elif sport == "ufc":
                st = stats.get(me["key"]) or {}
                for r in rows:
                    r.update({k: v for k, v in (st.get(r.get("ev")) or {}).items()})
                withst = [r for r in rows if r.get("ssl") is not None]
                if len(withst) >= 3:            # UFC fights with full numbers: use those for every stat
                    rows = withst
                    p.update(dates=[r["date"] for r in rows], ncur=len(rows), mu=p["mu"][:len(rows)])
                    p[win] = [r["win"] for r in rows]
                    p["ussl"] = [r["ssl"] for r in rows]
                    p["utsl"] = [r.get("tsl") or 0 for r in rows]
                    p["utd"] = [r.get("tdl") or 0 for r in rows]
                    p["ukd"] = [r.get("kd") or 0 for r in rows]
                    p["min"] = [r["mins"] for r in rows]
                    wins = sum(r["win"] for r in rows)
                pros, cons = ufc_case(me, op, rows, [r for r in hist.get(op["key"], []) if True], prof.get(me["key"]), prof.get(op["key"]))
                if pros:
                    p["info"].append("Why " + me["name"].split(" ")[-1] + " can win: " + " ".join(pros))
                p["risks"] = cons
                p["ufin"] = [r["fin"] for r in rows]
                pr = prof.get(me["key"]) or {}
                if pr.get("wld"):
                    p["info"].append(f"Pro record {pr['wld']}" + (f", {pr['ko'].split('-')[0]} KO/TKO wins" if pr.get("ko") else "") +
                                     (f", {pr['sub'].split('-')[0]} submission wins" if pr.get("sub") else "") + ".")
                bits = [x for x in (f"reach {pr['reach']}" if pr.get("reach") else "", f"age {pr['age']}" if pr.get("age") else "",
                                    pr.get("stance") or "") if x]
                if bits:
                    p["info"].append("Profile: " + ", ".join(bits) + ".")
                fins = sum(r["fin"] for r in rows)
                lostfin = sum(r["lostfin"] for r in rows)
                p["info"].append(f"Won {wins} of the last {len(rows)} fights, {fins} by finish." + (f" Lost by finish {lostfin} times." if lostfin else " Never finished in these fights."))
                p["finrate"] = round(fins / max(1, wins), 2) if wins else 0
                p["lostfin"] = lostfin
            else:
                p["info"].append(f"Won {wins} of their last {len(rows)} {m['league']} matches on Kalshi's record.")
            last5 = rows[:5]
            p["form5"] = sum(r["win"] for r in last5)
            players.append(p)
            net[me["key"]] = {"m": round(wins / len(rows) - 0.5, 2), "g": len(rows)}
    return {"sport": sport, "games": games, "players": players, "opp": {}, "why": {}, "net": net, "dates": dates, "teams": teams,
            "stats": {"sport": sport, "date": date_str, "today": today, "games": len(games), "teams": len(teams), "players": len(players),
                      "logs_current_season": sum(p["ncur"] for p in players), "logs_previous_season": 0,
                      "injuries": {"source": "none", "listed": 0, "hidden": 0},
                      "sources": {"scoreboard": src, "current_logs": "espn" if sport != "esp" else "kalshi", "previous_logs": "none"},
                      "updated": dt.datetime.now(ET).isoformat(timespec="seconds")}}


def lines(sport, games):
    """For /api/lines: every competitor Kalshi lists a win market for."""
    players, posted = {}, {}
    names = {}
    for g in games or []:
        for k in (g.get("a"), g.get("h")):
            posted[k] = False
    try:
        evs = kalshi_matches(sport)
    except Exception:
        evs = []
    stat = WIN[sport]
    for e in evs:
        for sd in e["sides"]:
            names[norm(sd["name"])] = True
            players[f"{norm(sd['name'])}|{stat}"] = [1]
    return players, posted, names


_ID = {}


def box(gid):
    """Results for tracked parlays: who won. tennis/esports read Kalshi's settled market, UFC reads ESPN."""
    sport, rest = str(gid).split(":", 1)
    out = {"status": 1, "text": "", "players": {}}
    if sport == "ufc":
        j = _get(SITE + "/mma/ufc/scoreboard")
        for e in j.get("events") or []:
            for c in e.get("competitions") or []:
                if str(c.get("id")) != rest:
                    continue
                stt = ((c.get("status") or {}).get("type") or {})
                out["status"] = 3 if stt.get("state") == "post" else 2 if stt.get("state") == "in" else 1
                out["text"] = stt.get("shortDetail") or ""
                for x in c.get("competitors") or []:
                    if out["status"] == 3:
                        out["players"][str(x.get("id"))] = {"uw": 1 if x.get("winner") else 0, "played": True, "min": ""}
        return out
    try:
        ms = _get(KAL + "/markets", {"event_ticker": rest, "limit": 10}).get("markets") or []
    except Exception:
        return out
    done = [m for m in ms if m.get("result") in ("yes", "no")]
    if done:
        out["status"], out["text"] = 3, "Final"
        for m in done:
            nm = m.get("yes_sub_title") or ""
            if sport == "esp":
                pid = "es" + slug(nm)
            else:
                pid = _ID.get(norm(nm)) or espn_athlete_id(nm, "tennis")
                _ID[norm(nm)] = pid
            if pid:
                out["players"][str(pid)] = {("ew" if sport == "esp" else "tw"): 1 if m["result"] == "yes" else 0, "played": True, "min": ""}
    elif any(m.get("status") in ("closed",) for m in ms):
        out["status"], out["text"] = 2, "In progress"
    return out
