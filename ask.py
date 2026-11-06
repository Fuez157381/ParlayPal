"""Parlay Pal's AI brain: answers with Claude, which can look up any player's real games while it thinks.
Uses Claude if ANTHROPIC_API_KEY is set. Otherwise a free AI: GEMINI_API_KEY (Google AI Studio) first, then GROQ_API_KEY.
If none is set, or the free ones are out of uses, Pal uses his built-in brain in the page.
Model comes from anthropic_model.txt, default below."""
import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.request

try:
    import anthropic
except ImportError:  # only needed for the paid Claude brain
    anthropic = None

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL_DEFAULT = "claude-opus-5-5"
MAX_LOOKUPS = 10

SYSTEM = """You are Pal, the mascot inside Parlay Pal, a free NBA player-prop research site. You help people understand players, matchups and parlays, and you change their parlay when they ask.

Getting the facts
- The CONTEXT in each message holds the day's games, the user's parlay, the strongest picks ("pool") and the injury report. Treat it as live and correct.
- For any player or team not fully covered there, call get_player or get_team instead of answering from memory. Look before you say you don't have something. Work out hit rates, averages and trends from the games you get back, and show the numbers that matter.
- Only use numbers from the context or your lookups for this season's stats, lines and injuries. Never invent odds, prices, lines or stats. If the data doesn't have it, say so.
- For general basketball or betting knowledge, such as history or how a bet type works, answer from what you know and mention when it could be out of date.

Giving advice
- Answer the question that was asked, directly, then give the reason. If someone asks whether to take a line, say what you'd do and why: how often he cleared it, his average against the line, his recent form, minutes, opponent and blowout risk.
- Be honest. Parlays are risky and past games don't promise anything. Never call anything a lock or a guarantee, and never push someone to bet more or add more legs. If someone sounds like they're chasing losses or betting more than they can afford, gently say so and mention the National Problem Gambling Helpline, 1-800-522-4700.
- Never recommend a player listed as out or doubtful. You can discuss questionable players, but flag them.
- Use at most one leg per player unless asked, and no more than 3 legs from one team. Say so when legs could fail together, such as several from one game.

Changing the parlay
- Change it only when the user asks. Check each request against the data first. If it can't be done, say why and offer the closest real alternative.
- Refer to picks by their "ref" from the context. For a player who isn't in the pool, use player, stat and bar.
- Describe in your reply what you changed and why.
- When the user names an app ("only Kalshi", "on Underdog", "legs Hard Rock has"), call check_apps on your best candidates (20 at a time, more rounds if needed) and add only legs that app lists. Also set the platform. If the app doesn't list enough strong legs, say how many it has instead of padding with weak ones or legs from other apps.

Voice
- Friendly and relaxed, like a friend who really knows the numbers. Light humor is fine, one joke at most, and never at the user's expense.
- Keep simple answers to 2 to 5 sentences. Go deeper when someone asks for a breakdown. Plain text with "- " lists, no markdown. Write "3-Pointers" for threes.
- When a user's message refers to something earlier, use the conversation to work out what they mean. "This guy", "him" or "he" means the player you were just talking about (lastPlayerDiscussed), or the player whose page is open (playerPageOpen). Only ask who they mean if neither exists.
- People talk casually, by voice, with typos and slang ("is bro cooking", "gimme a 10 leg", "fade him"). Work out what they want and do it. Do exactly what was asked: the number of legs, the app, the stat and the team they named.

Your final answer is the JSON object the response format asks for. Use an empty actions list when nothing should change. Action fields you don't need are null. Actions: "add" (ref, or player + stat + bar), "remove" (ref or player), "clear", "stake" (value), "platform" (value). Stat codes: pts, reb, ast, tpm."""

TOOLS = [
    {
        "name": "get_player",
        "description": "Get a player's last 10 games (points, rebounds, assists, 3-Pointers, minutes, dates; newest first), his team, and injury status. Use it for any question about a specific player, including whether a line is worth taking.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Player name, full or partial"}},
            "required": ["name"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "get_team",
        "description": "Get a team's game on this day, its average scoring margin, how it treats each stat on defense, and each player's averages and injury status. Use it for questions about a team or a matchup.",
        "input_schema": {
            "type": "object",
            "properties": {"team": {"type": "string", "description": "Three-letter team code, like BOS or LAL"}},
            "required": ["team"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "check_apps",
        "description": "Check which betting apps list each leg right now, with each app's American odds for the over. Use it whenever the user asks for legs on a specific app, wants only legs that are really offered, or asks for odds. Up to 20 legs per call. Copy team and vs from the pool or slip.",
        "input_schema": {
            "type": "object",
            "properties": {
                "legs": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "player": {"type": "string"},
                            "team": {"type": "string", "description": "Three-letter code"},
                            "vs": {"type": "string", "description": "Opponent three-letter code"},
                            "stat": {"type": "string", "enum": ["pts", "reb", "ast", "tpm"]},
                            "bar": {"type": "number", "description": "The bar, e.g. 20 for 20+"},
                        },
                        "required": ["player", "team", "vs", "stat", "bar"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["legs"],
            "additionalProperties": False,
        },
        "strict": True,
    },
]

REPLY_FORMAT = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "reply": {"type": "string"},
            "actions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "op": {"type": "string", "enum": ["add", "remove", "clear", "stake", "platform"]},
                        "ref": {"type": ["string", "null"]},
                        "player": {"type": ["string", "null"]},
                        "stat": {"type": ["string", "null"], "enum": ["pts", "reb", "ast", "tpm", None]},
                        "bar": {"type": ["number", "null"]},
                        "value": {"type": ["string", "null"]},
                    },
                    "required": ["op", "ref", "player", "stat", "bar", "value"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["reply", "actions"],
        "additionalProperties": False,
    },
}


def api_key():
    k = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if k:
        return k
    p = os.path.join(HERE, "anthropic_key.txt")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return f.read().strip()
    return ""


# Free brains, tried in order when no Claude key is set. Each uses the OpenAI-style chat API.
FREE = [
    ("GROQ_API_KEY", "https://api.groq.com/openai/v1/chat/completions", "GROQ_MODEL", "auto"),
    ("GEMINI_API_KEY", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions", "GEMINI_MODEL", "gemini-flash-lite-latest,gemini-flash-latest,gemini-3.8-flash"),
]


def _env_or_file(name):
    k = os.environ.get(name, "").strip()
    if k:
        return k
    p = os.path.join(HERE, name.lower() + ".txt")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return f.read().strip()
    return ""


def free_brains():
    lists = []
    for name, url, mv, md in FREE:
        k = _env_or_file(name)
        if k:
            lists.append([(k, url, m.strip()) for m in (os.environ.get(mv, "").strip() or md).split(",") if m.strip()])
    out = []  # take turns between the free AIs, so a busy one doesn't hold everything up
    for i in range(max([len(l) for l in lists] or [0])):
        out += [l[i] for l in lists if i < len(l)]
    return out


def configured():
    return bool(api_key() or free_brains())


def model():
    p = os.path.join(HERE, "anthropic_model.txt")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            m = f.read().strip()
            if m:
                return m
    return MODEL_DEFAULT


def _norm(s):
    s = unicodedata.normalize("NFD", str(s)).encode("ascii", "ignore").decode().lower()
    return " ".join("".join(c if c.isalnum() else " " for c in s).split())


def _avg(v):
    return round(sum(v) / len(v), 1) if v else None


def _player_row(p):
    keep = ("n", "t", "pts", "reb", "ast", "tpm", "min", "dates", "inj")
    return {k: (p[k][:10] if isinstance(p.get(k), list) else p.get(k)) for k in keep if k in p}


def get_player(data, name):
    want = _norm(name)
    players = data.get("players", [])
    hits = [p for p in players if _norm(p.get("n", "")) == want] or [p for p in players if want and want in _norm(p.get("n", ""))]
    if not hits and want:
        words = set(want.split())
        hits = [p for p in players if words & set(_norm(p.get("n", "")).split())]
    if not hits:
        return {"found": False, "note": f"No player matching {name!r} plays on this day. He may have no game, or the name may be different."}
    hits.sort(key=lambda p: -(_avg((p.get("min") or [])[:10]) or 0))
    out = {"found": True, "player": _player_row(hits[0])}
    if len(hits) > 1:
        out["otherMatches"] = [p.get("n") for p in hits[1:5]]
    return out


def get_team(data, team):
    code = str(team).strip().upper()
    games = [g for g in data.get("games", []) if code in (g.get("a"), g.get("h"))]
    rows = [p for p in data.get("players", []) if p.get("t") == code]
    if not games and not rows:
        return {"found": False, "note": f"{code} has no game on this day."}
    rows.sort(key=lambda p: -(_avg((p.get("min") or [])[:10]) or 0))
    return {
        "found": True,
        "team": code,
        "game": games[0] if games else None,
        "margin": (data.get("net") or {}).get(code),
        "statsAllowedVsAverage": (data.get("opp") or {}).get(code),
        "players": [
            {"n": p.get("n"), "inj": p.get("inj"), "min": _avg((p.get("min") or [])[:10]),
             **{c: _avg((p.get(c) or [])[:10]) for c in ("pts", "reb", "ast", "tpm")}}
            for p in rows
        ],
    }


def check_apps(legs, compare):
    legs = [{"n": l.get("player"), "t": l.get("team"), "opp": l.get("vs"), "s": l.get("stat"), "bar": l.get("bar")} for l in (legs or [])[:20]]
    res = compare(legs)
    books = res.get("books") or []
    rows = []
    for i, l in enumerate(legs):
        apps = {b.get("title") or b.get("key"): b["legs"][i] for b in books if b.get("legs") and i < len(b["legs"]) and b["legs"][i] is not None}
        rows.append({"player": l["n"], "stat": l["s"], "bar": l["bar"], "apps": apps})
    return {"legs": rows, "note": "An app missing from a leg does not list that line right now. Prices are American odds for the over."}


def _run_tool(name, args, load_day, compare=None):
    try:
        if name == "check_apps":
            if not compare:
                return {"error": "App checking isn't available here."}
            return check_apps(args.get("legs"), compare)
        data = load_day()
        if name == "get_player":
            return get_player(data, args.get("name", ""))
        if name == "get_team":
            return get_team(data, args.get("team", ""))
        return {"error": f"unknown tool {name}"}
    except Exception as e:  # the model can still answer from the context
        return {"error": f"Lookup failed: {type(e).__name__}"}


def _history(history):
    """Turn the chat log into alternating turns that start with the user."""
    msgs = []
    for h in (history or [])[-12:]:
        role = "user" if h.get("r") == "u" else "assistant"
        t = str(h.get("t", ""))[:4000]
        if not t:
            continue
        if msgs and msgs[-1]["role"] == role:
            msgs[-1]["content"] += "\n" + t
        else:
            msgs.append({"role": role, "content": t})
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    return msgs


def _user_turns(message, history, context):
    msgs = _history(history)
    user = "CONTEXT (live from the site):\n" + json.dumps(context, separators=(",", ":")) + "\n\nUSER MESSAGE: " + str(message)[:4000]
    if msgs and msgs[-1]["role"] == "user":
        msgs[-1]["content"] += "\n" + user
    else:
        msgs.append({"role": "user", "content": user})
    return msgs


def _finish(text):
    text = (text or "").strip()
    m = re.search(r"\{.*\}", text, re.S)
    try:
        out = json.loads(m.group(0) if m else text)
        if not isinstance(out, dict):
            raise ValueError
    except ValueError:
        return {"configured": True, "reply": text, "actions": []}
    acts = [{k: v for k, v in a.items() if v is not None} for a in (out.get("actions") or []) if isinstance(a, dict) and a.get("op")]
    return {"configured": True, "reply": str(out.get("reply", "")).strip(), "actions": acts[:12]}


FREE_FORMAT = """

Reply format: your final message must be ONLY a JSON object, no other text: {"reply": "what you say to the user", "actions": [{"op": "add", "ref": "...", "player": null, "stat": null, "bar": null, "value": null}]}. Use "actions": [] when nothing should change."""

def _plain(x):
    """Drop schema words some free AIs reject."""
    if isinstance(x, dict):
        return {k: _plain(v) for k, v in x.items() if k != "additionalProperties"}
    if isinstance(x, list):
        return [_plain(v) for v in x]
    return x


OAI_TOOLS = [{"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": _plain(t["input_schema"])}} for t in TOOLS]


DEADLINE = [0.0]  # stop trying free AIs before Vercel's 60-second limit


def _post(url, key, body):
    left = DEADLINE[0] - time.time()
    if left < 4:
        raise TimeoutError("out of time")
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "Authorization": "Bearer " + key, "User-Agent": "ParlayPal/1.0"})
    with urllib.request.urlopen(req, timeout=min(20, left)) as r:
        return json.loads(r.read().decode())


PREFER = ("gpt-oss-120b", "llama-4-maverick", "kimi", "qwen3", "llama-4", "70b", "gpt-oss-20b")


def _pick_model(url, key):
    """Ask the AI service which models it has now and pick the strongest one we know of."""
    req = urllib.request.Request(url.replace("/chat/completions", "/models"), headers={"Authorization": "Bearer " + key, "User-Agent": "ParlayPal/1.0"})
    with urllib.request.urlopen(req, timeout=8) as r:
        ids = [m.get("id", "") for m in json.loads(r.read().decode()).get("data", [])]
    print("free AI models:", ids)
    for want in PREFER:
        for i in ids:
            if want in i.lower() and not any(x in i.lower() for x in ("guard", "whisper", "tts", "safeguard")):
                return i
    raise ValueError("no usable model")


def _ask_free(key, url, mdl, message, history, context, load_day, compare):
    if mdl == "auto":
        mdl = _pick_model(url, key)
    ctx = dict(context or {})
    if isinstance(ctx.get("pool"), list):
        ctx["pool"] = ctx["pool"][:45]  # free plans allow fewer words per minute
    msgs = [{"role": "system", "content": SYSTEM + FREE_FORMAT}] + _user_turns(message, history, ctx)
    lookups = 0
    for _ in range(6):
        body = {"model": mdl, "messages": msgs, "temperature": 0.4, "max_tokens": 4000}
        if "generativelanguage" in url:
            body["reasoning_effort"] = "low"  # quicker answers
        if lookups <= MAX_LOOKUPS:
            body["tools"] = OAI_TOOLS
        res = _post(url, key, body)
        m = res["choices"][0]["message"]
        calls = m.get("tool_calls") or []
        if not calls:
            out = _finish(m.get("content"))
            out["brain"] = mdl
            return out
        msgs.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": calls})
        for c in calls:
            lookups += 1
            try:
                args = json.loads(c["function"].get("arguments") or "{}")
            except ValueError:
                args = {}
            out = _run_tool(c["function"]["name"], args, load_day, compare) if lookups <= MAX_LOOKUPS else {"error": "Lookup limit reached. Answer now."}
            msgs.append({"role": "tool", "tool_call_id": c.get("id"), "content": json.dumps(out, separators=(",", ":"))})
    msgs.append({"role": "user", "content": "Answer now with what you have, as the JSON object."})
    res = _post(url, key, {"model": mdl, "messages": msgs, "temperature": 0.4, "max_tokens": 4000})
    out = _finish(res["choices"][0]["message"].get("content"))
    out["brain"] = mdl
    return out


def ask(message, history, context, load_day=None, compare=None):
    load_day = load_day or (lambda: {})
    if api_key():
        return _ask_claude(message, history, context, load_day, compare)
    err = None
    DEADLINE[0] = time.time() + 48
    for key, url, mdl in free_brains():
        try:
            out = _ask_free(key, url, mdl, message, history, context, load_day, compare)
            out.setdefault("brain", mdl)
            return out
        except (urllib.error.URLError, KeyError, IndexError, ValueError, TimeoutError) as e:
            err = e  # out of free uses or down: try the next one
            detail = e.read().decode()[:300] if isinstance(e, urllib.error.HTTPError) else str(e)
            print("free AI failed:", url, mdl, detail)
            if isinstance(e, TimeoutError) and str(e) == "out of time":
                break
    if err:
        return {"configured": True, "error": "the free AI is busy, try again in a minute"}
    return {"configured": False}


def _ask_claude(message, history, context, load_day=None, compare=None):
    key = api_key()
    client = anthropic.Anthropic(api_key=key, timeout=50.0, max_retries=1)
    msgs = _user_turns(message, history, context)

    params = {
        "model": model(),
        "max_tokens": 16000,
        "system": [{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
        "tools": TOOLS,
        "output_config": {"format": REPLY_FORMAT, "effort": "medium"},
    }
    create = client.messages.create
    if model() in ("claude-opus-5-5", "claude-fable-5-1", "claude-opus-5", "claude-sonnet-5-5"):
        # if a safety check declines a harmless question, another model answers instead of Pal going quiet
        params.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        create = client.beta.messages.create

    lookups = 0
    while True:
        res = create(messages=msgs, **params)
        if res.stop_reason == "refusal":
            return {"configured": True, "reply": "I can't help with that one. Ask me about a player, a matchup or your parlay.", "actions": []}
        if res.stop_reason == "max_tokens":
            return {"configured": True, "error": "the answer ran too long"}
        if res.stop_reason == "pause_turn":
            msgs.append({"role": "assistant", "content": res.content})
            continue
        calls = [b for b in res.content if b.type == "tool_use"]
        if not calls:
            break
        msgs.append({"role": "assistant", "content": res.content})
        lookups += len(calls)
        results = []
        for c in calls:
            if lookups > MAX_LOOKUPS:
                out = {"error": "Lookup limit reached. Answer now with what you have, and say what you couldn't check."}
            else:
                out = _run_tool(c.name, c.input, load_day, compare)
            results.append({"type": "tool_result", "tool_use_id": c.id, "content": json.dumps(out, separators=(",", ":"))})
        msgs.append({"role": "user", "content": results})

    text = next((b.text for b in res.content if b.type == "text"), "")
    try:
        out = json.loads(text)
    except ValueError:
        return {"configured": True, "reply": text.strip(), "actions": []}
    acts = [{k: v for k, v in a.items() if v is not None} for a in out.get("actions", []) if isinstance(a, dict)]
    return {"configured": True, "reply": str(out.get("reply", "")).strip(), "actions": acts[:12]}
