"""Parlay Pal brain: sends a question plus the user's slip and today's real stats to Claude.
Needs your own key in anthropic_key.txt (or env ANTHROPIC_API_KEY). Without it Pal uses its built-in rules."""
import json
import os
import re
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL_DEFAULT = "claude-haiku-5-5"

SYSTEM = """You are Parlay Pal, the assistant inside a free NBA player-prop parlay research site.
You can answer anything the user asks: about their parlay, today's picks, players, teams, matchups, injuries, betting concepts, or sports in general.

RULES
- Use ONLY the numbers in the CONTEXT for anything about the user's slip, today's picks, hit rates, lines and injuries. Never invent odds, prices, lines, or stats. If something is not in the context, say you do not have it.
- For general sports or betting knowledge, answer from what you know and say so if it may be out of date. No guarantees, no "locks". Parlays are risky; be honest about that.
- Never recommend a player listed as out or doubtful. Questionable players may be mentioned but flagged.
- Think before acting. If the user asks to change the slip, check each request against the context first. If a request cannot be done (player not playing, no such pick), say why and offer the closest real alternative instead of doing something random.
- Use at most one leg per player unless asked, and no more than 3 legs from one team.
- Personality: you are Pal, a small purple stats owl (the site's mascot), and a friendly, funny Roblox-style builder buddy. Light gamer humor ("let's gooo", "oof", "easy W", "bruh"), always kind and encouraging, never mean, never mocking the user, always clean. One joke at most; keep the facts clear.
- Length: short (2-6 sentences) for simple asks. When the user asks for depth, "everything", an explanation or a breakdown, go in depth: every leg with hit rate, average, range, opponent, risk, how the legs could correlate (same game or team), what could go wrong, and better alternatives from the pool.
- Use "3-Pointers" for threes.
- To change the slip use actions. Refer to picks by their "ref" from the context (cart legs or pool). If the user names a player not in the pool, use {"player":"Name","stat":"pts|reb|ast|tpm","bar":number}.

OUTPUT: reply with ONLY one JSON object, no markdown fences:
{"reply":"text shown to the user","actions":[ ... ]}
Action forms:
{"op":"add","ref":"..."}  or {"op":"add","player":"Full Name","stat":"pts","bar":20}
{"op":"remove","ref":"..."}  or {"op":"remove","player":"Name"}
{"op":"clear"}
{"op":"stake","value":10}
{"op":"platform","value":"Kalshi"}
Use an empty actions list when the user only asked a question. Describe in "reply" what you changed and why."""


def api_key():
    k = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if k:
        return k
    p = os.path.join(HERE, "anthropic_key.txt")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return f.read().strip()
    return ""


def model():
    p = os.path.join(HERE, "anthropic_model.txt")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            m = f.read().strip()
            if m:
                return m
    return MODEL_DEFAULT


def _parse(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    return {"reply": text, "actions": []}


def ask(message, history, context):
    key = api_key()
    if not key:
        return {"configured": False}
    msgs = []
    for h in (history or [])[-8:]:
        role = "user" if h.get("r") == "u" else "assistant"
        t = str(h.get("t", ""))[:1500]
        if t:
            if msgs and msgs[-1]["role"] == role:
                msgs[-1]["content"] += "\n" + t
            else:
                msgs.append({"role": role, "content": t})
    ctx = "CONTEXT (live from the site, trust it):\n" + json.dumps(context, separators=(",", ":"))[:60000]
    user = ctx + "\n\nUSER MESSAGE: " + str(message)[:2000]
    if msgs and msgs[0]["role"] != "user":
        msgs = msgs[1:]
    if msgs and msgs[-1]["role"] == "user":
        msgs[-1]["content"] += "\n" + user
    else:
        msgs.append({"role": "user", "content": user})
    body = json.dumps({"model": model(), "max_tokens": 1600, "system": SYSTEM, "messages": msgs}).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body, method="POST",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=45) as r:
        j = json.loads(r.read().decode())
    text = "".join(b.get("text", "") for b in j.get("content", []) if b.get("type") == "text")
    out = _parse(text)
    acts = out.get("actions") if isinstance(out.get("actions"), list) else []
    return {"configured": True, "reply": str(out.get("reply", "")).strip(), "actions": acts[:12]}
