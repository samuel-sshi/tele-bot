# api/poll.py
import os, json, hashlib
from typing import Dict, Any, Set, List
from fastapi import FastAPI
from upstash_redis import Redis
import httpx
from html import escape as H

API_URL = "https://gagstock.gleeze.com/grow-a-garden"
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG = f"https://api.telegram.org/bot{BOT_TOKEN}"

app = FastAPI()
redis = Redis(url=os.environ["kv_KV_REST_API_URL"], token=os.environ["kv_KV_REST_API_TOKEN"])

def b(x): return f"<b>{H(x)}</b>"
def code(x): return f"<code>{H(x)}</code>"
def li(x): return f"• {H(x)}"

def hash_payload(data: Dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

def fmt_cat(name: str, payload: Dict[str, Any]) -> str:
    items = (payload or {}).get("items", [])
    cd = (payload or {}).get("countdown")
    parts = [b(name.capitalize())]
    if cd: parts.append(f"Refresh in: {code(str(cd))}")
    if items:
        for it in items:
            nm = it.get("name", "?"); qty = it.get("quantity", "?"); emoji = it.get("emoji","")
            parts.append(li(f"{emoji} {nm} ×{qty}"))
    else:
        parts.append("<i>No items</i>")
    return "\n".join(parts)

def fmt_msg(payload: Dict[str, Any], updated_at: str) -> str:
    data = payload.get("data") or {}
    order = ["egg","gear","seed","honey","cosmetics","travelingmerchant"]
    sections = [fmt_cat(k, data[k]) for k in order if k in data] + \
               [fmt_cat(k, v) for k,v in data.items() if k not in order]
    return f"{b('GAG Stock Update')}\nupdated_at: {code(updated_at)}" + ("\n\n" + "\n\n".join(sections) if sections else "")

async def send_all(chat_ids: List[int], text: str):
    async with httpx.AsyncClient(timeout=20) as c:
        for cid in chat_ids:
            await c.post(f"{TG}/sendMessage", json={"chat_id": cid, "text": text, "parse_mode": "HTML"})

@app.get("/")
async def run():
    # fetch
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(API_URL, headers={"Accept":"application/json"})
        data = r.json()
        if isinstance(data, str):
            data = json.loads(data)

    updated_at = data.get("updated_at") or "unknown"
    inner = data.get("data") or {}
    h = hash_payload(inner)

    state = redis.get("state") or {"updated_at": None, "hash": None}
    changed = (state.get("updated_at") != updated_at) or (state.get("hash") != h) or (state.get("updated_at") is None)

    if changed:
        msg = fmt_msg(data, updated_at)
        subs = redis.get("subs") or []
        await send_all(list(subs), msg)
        redis.set("state", {"updated_at": updated_at, "hash": h})

    return {"ok": True, "changed": bool(changed), "updated_at": updated_at}
