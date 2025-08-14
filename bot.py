import os
import json
import asyncio
import hashlib
import logging
import textwrap
from typing import Dict, Any, Set

import aiohttp
from aiohttp import ClientTimeout
from html import escape as html_escape

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes


# === Config ===
API_URL = "https://gagstock.gleeze.com/grow-a-garden"
POLL_SECONDS = 60  # be nice to the API
SUBS_FILE = "subscribers.json"
STATE_FILE = "last_state.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s"
)
log = logging.getLogger("gagstock-bot")


# === Persistence helpers ===
def load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path: str, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def load_subscribers() -> Set[int]:
    return set(load_json(SUBS_FILE, []))

def save_subscribers(subs: Set[int]):
    save_json(SUBS_FILE, list(subs))

def load_last_state() -> Dict[str, Any]:
    return load_json(STATE_FILE, {"updated_at": None, "hash": None})

def save_last_state(state: Dict[str, Any]):
    save_json(STATE_FILE, state)


# === Utility ===
def hash_payload(data: Dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()

# HTML-safe helpers (avoid Telegram Markdown entity issues)
def b(x: str) -> str:
    return f"<b>{html_escape(x)}</b>"

def code(x: str) -> str:
    return f"<code>{html_escape(x)}</code>"

def li(x: str) -> str:
    return f"• {html_escape(x)}"


# === Message formatting based on documented response shape ===
def format_category(name: str, payload: Dict[str, Any]) -> str:
    items = payload.get("items", []) if isinstance(payload, dict) else []
    cd = payload.get("countdown") if isinstance(payload, dict) else None
    header = b(name.capitalize())

    # Special-case "travelingmerchant"
    if name.lower() == "travelingmerchant":
        status = payload.get("status") if isinstance(payload, dict) else None
        appear = payload.get("appearIn") if isinstance(payload, dict) else None
        merchant = payload.get("merchantName") if isinstance(payload, dict) else None
        parts = [header]
        if merchant: parts.append(f"Merchant: <i>{html_escape(str(merchant))}</i>")
        if status: parts.append(f"Status: {code(str(status))}")
        if appear: parts.append(f"Appears in: {code(str(appear))}")
        if items and isinstance(items, list):
            parts.append("Items:")
            for it in items:
                if not isinstance(it, dict): continue
                nm = it.get("name", "?")
                qty = it.get("quantity", "?")
                emoji = it.get("emoji", "")
                parts.append(li(f"{emoji} {nm} ×{qty}"))
        else:
            parts.append("<i>No items</i>")
        return "\n".join(parts)

    # Standard categories
    parts = [header]
    if cd: parts.append(f"Refresh in: {code(str(cd))}")
    if items and isinstance(items, list):
        for it in items:
            if not isinstance(it, dict): continue
            nm = it.get("name", "?")
            qty = it.get("quantity", "?")
            emoji = it.get("emoji", "")
            parts.append(li(f"{emoji} {nm} ×{qty}"))
    else:
        parts.append("<i>No items</i>")
    return "\n".join(parts)

def format_message(payload: Dict[str, Any], updated_at: str) -> str:
    data = payload.get("data", {}) if isinstance(payload, dict) else {}
    if not isinstance(data, dict):
        data = {}

    sections = []
    preferred_order = ["egg", "gear", "seed", "honey", "cosmetics", "travelingmerchant"]
    for key in preferred_order:
        if key in data:
            sections.append(format_category(key, data.get(key, {})))
    for key in data:
        if key not in preferred_order:
            sections.append(format_category(key, data.get(key, {})))

    header = f"{b('GAG Stock Update')}\nupdated_at: {code(updated_at)}"
    return header + ("\n\n" + "\n\n".join(sections) if sections else "")


# === Network ===
async def fetch_api(session: aiohttp.ClientSession) -> Dict[str, Any]:
    """
    Robustly fetch and parse the API, handling cases where the server returns:
      - a JSON object (expected)
      - a JSON string (sometimes containing JSON again)
      - non-JSON / HTML or other errors
    """
    headers = {
        "Accept": "application/json",
        "User-Agent": "gagstock-telegram-bot/1.0"
    }
    async with session.get(API_URL, headers=headers, timeout=ClientTimeout(total=15)) as resp:
        text = await resp.text()
        # First parse attempt
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            raise RuntimeError(f"Non-JSON response ({resp.status} {resp.content_type}): {text[:200]!r}")

        # If it's a JSON string, try to parse the inner content
        if isinstance(data, str):
            try:
                maybe = json.loads(data)
                if isinstance(maybe, dict):
                    return maybe
            except Exception:
                pass
            raise RuntimeError(f"Unexpected JSON string: {data[:200]!r}")

        if not isinstance(data, dict):
            raise RuntimeError(f"Unexpected JSON type: {type(data)}; head: {str(data)[:200]!r}")

        return data


# === Broadcast ===
async def broadcast(application: Application, text: str):
    if not SUBSCRIBERS:
        return
    tasks = [
        application.bot.send_message(chat_id=cid, text=text, parse_mode=ParseMode.HTML)
        for cid in SUBSCRIBERS
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for r in results:
        if isinstance(r, Exception):
            log.warning("Send failed: %s", r)


# === Watcher loop ===
async def watcher(application: Application):
    state = load_last_state()
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                payload = await fetch_api(session)

                if not isinstance(payload, dict) or "data" not in payload:
                    log.error("Bad payload shape: %s", str(payload)[:200])
                    await asyncio.sleep(POLL_SECONDS)
                    continue

                updated_at = payload.get("updated_at")
                data = payload.get("data") or {}
                if not isinstance(data, dict):
                    log.error("Bad data field: %s", str(data)[:200])
                    await asyncio.sleep(POLL_SECONDS)
                    continue

                payload_hash = hash_payload(data)

                changed = False
                reason = ""
                if updated_at and updated_at != state.get("updated_at"):
                    changed = True; reason = "timestamp"
                elif payload_hash != state.get("hash"):
                    changed = True; reason = "content-hash"

                # Initial boot: push once if we have a timestamp
                if state.get("updated_at") is None and updated_at:
                    changed = True; reason = "initial"

                if changed:
                    msg = format_message(payload, updated_at or "unknown")
                    await broadcast(application, msg)
                    log.info("Broadcasted update to %d subscriber(s) (%s)", len(SUBSCRIBERS), reason)

                state = {"updated_at": updated_at, "hash": payload_hash}
                save_last_state(state)

            except Exception as e:
                log.error("Watcher error: %s", e)

            await asyncio.sleep(POLL_SECONDS)


# === Telegram handlers ===
SUBSCRIBERS: Set[int] = load_subscribers()

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(textwrap.dedent("""\
        Hi! I’ll notify you when GAG Stock updates.
        Commands:
        /subscribe – receive updates
        /unsubscribe – stop updates
        /status – show latest known timestamp
        /now – fetch and push the current stock immediately
    """))

async def cmd_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    SUBSCRIBERS.add(cid)
    save_subscribers(SUBSCRIBERS)
    await update.message.reply_text("Subscribed! You’ll get messages on updates. Use /now to get the latest instantly.")

async def cmd_unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    SUBSCRIBERS.discard(cid)
    save_subscribers(SUBSCRIBERS)
    await update.message.reply_text("Unsubscribed. You won’t receive further updates.")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = load_last_state()
    await update.message.reply_text(f"Last known updated_at: {st.get('updated_at') or 'unknown'}")

async def cmd_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        async with aiohttp.ClientSession() as session:
            payload = await fetch_api(session)
        msg = format_message(payload, payload.get("updated_at") or "unknown")
        await broadcast(context.application, msg)
        await update.message.reply_text("Pushed the latest stock to all subscribers.")
    except Exception as e:
        await update.message.reply_text(f"Fetch failed: {e}")

async def on_start(application: Application):
    application.create_task(watcher(application))


# === Main ===
def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN env var")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("subscribe", cmd_subscribe))
    app.add_handler(CommandHandler("unsubscribe", cmd_unsubscribe))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("now", cmd_now))

    # Launch watcher when the bot starts
    app.post_init = on_start

    log.info("Starting bot…")
    app.run_polling()

if __name__ == "__main__":
    main()
