# api/telegram.py
import os, json, textwrap
from typing import Set, Dict, Any
from fastapi import FastAPI, Request
from upstash_redis import Redis
import httpx

app = FastAPI()
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG = f"https://api.telegram.org/bot{BOT_TOKEN}"
redis = Redis(url=os.environ["kv_KV_REST_API_URL"], token=os.environ["kv_KV_REST_API_TOKEN"])

# ---- persistence helpers (Redis) ----
def get_subscribers() -> Set[int]:
    raw = redis.get("subs")
    return set(raw) if isinstance(raw, list) else set()

def save_subscribers(s: Set[int]):
    redis.set("subs", list(s))

def get_state() -> Dict[str, Any]:
    return redis.get("state") or {"updated_at": None, "hash": None}

def save_state(st: Dict[str, Any]):
    redis.set("state", st)

async def send_message(chat_id: int, text: str):
    async with httpx.AsyncClient(timeout=15) as c:
        await c.post(f"{TG}/sendMessage", json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"})

# ---- commands ----
@app.post("/")
async def webhook(request: Request):
    u = await request.json()
    msg = u.get("message") or u.get("edited_message") or {}
    chat = msg.get("chat", {})
    text = (msg.get("text") or "").strip()
    cid = chat.get("id")
    if not cid or not text.startswith("/"):
        return {"ok": True}

    subs = get_subscribers()

    if text.startswith("/start"):
        await send_message(cid, textwrap.dedent("""\
            Hi! I’ll notify you when GAG Stock updates.
            Commands:
            /subscribe – receive updates
            /unsubscribe – stop updates
            /status – show latest known timestamp
            /now – fetch and push the current stock immediately
        """))
    elif text.startswith("/subscribe"):
        subs.add(cid); save_subscribers(subs)
        await send_message(cid, "Subscribed! Use /now to get the latest instantly.")
    elif text.startswith("/unsubscribe"):
        subs.discard(cid); save_subscribers(subs)
        await send_message(cid, "Unsubscribed.")
    elif text.startswith("/status"):
        st = get_state()
        await send_message(cid, f"Last known updated_at: {st.get('updated_at') or 'unknown'}")
    elif text.startswith("/now"):
        # Kick the poller manually
        async with httpx.AsyncClient(timeout=15) as c:
            await c.get("https://" + os.environ["VERCEL_URL"] + "/api/poll")
        await send_message(cid, "Pushed the latest stock to all subscribers.")
    return {"ok": True}
