"""A tiny shared cache in the accounts database, so every server copy reuses the same answer
(each copy on Vercel otherwise asks the odds services again and burns the free allowance)."""
import json
import time


def get(k, max_age):
    try:
        import auth
        if not auth.pg_url():
            return None
        with auth.db() as con:
            con.execute("CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, t BIGINT NOT NULL, v TEXT NOT NULL)")
            r = con.execute("SELECT t, v FROM kv WHERE k=?", (k,)).fetchone()
            if r and time.time() - r["t"] < max_age:
                return json.loads(r["v"])
    except Exception:
        return None
    return None


def put(k, v):
    try:
        import auth
        if not auth.pg_url():
            return
        with auth.db() as con:
            con.execute("CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, t BIGINT NOT NULL, v TEXT NOT NULL)")
            con.execute("DELETE FROM kv WHERE k=?", (k,))
            con.execute("INSERT INTO kv(k, t, v) VALUES(?,?,?)", (k, int(time.time()), json.dumps(v)))
    except Exception:
        pass
