#!/usr/bin/env python3
"""
Telegram bot "Faye" (2025‑06‑16‑d)
Tweaks
• Slower, more human‑like delivery:   ‑ waits 1–2 s “thinking” pause before first chunk   ‑ typing speed 0.06–0.10 s per char, min 1 s, max 6 s per chunk
• Idle window still 15 s; abort logic unchanged
• Global model vars: FAYE_MODEL & MEMORY_MODEL
"""
import os, sys, json, logging, asyncio, random, re
from json import JSONDecodeError
from typing import Dict
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, filters
)
from telegram.error import Conflict
from openai import OpenAI, OpenAIError

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not TELEGRAM_TOKEN or not OPENAI_API_KEY:
    sys.exit("set TELEGRAM_TOKEN and OPENAI_API_KEY env vars")

IDLE_SECONDS = 15
SYSTEM_PROMPT = open("faye.txt", encoding="utf-8").read().strip()
SHORT_MEMORY_FILE = "short_memory.json"
LONG_MEMORY_FILE  = "long_memory.json"

# ── global config ────────────────────────────────────────────────────────
FAYE_MODEL   = "gpt-4o-mini"   # conversational model
MEMORY_MODEL = "gpt-4o-mini"   # memory extraction model
USE_MEMORY   = True

# ── helpers to load memory lists ─────────────────────────────────────────

def load_list(path:str):
    try:
        with open(path,encoding="utf-8") as f:
            d=json.load(f)
            if isinstance(d,list):
                return d
    except (FileNotFoundError,JSONDecodeError):
        pass
    with open(path,"w",encoding="utf-8") as f:
        json.dump([],f)
    return []

short_mem = load_list(SHORT_MEMORY_FILE)
long_mem  = load_list(LONG_MEMORY_FILE)

client = OpenAI(api_key=OPENAI_API_KEY)

# ── runtime state ─────────────────────────────────────────────────────────
msg_counter: Dict[int,int] = {}
last_replied: Dict[int,int] = {}   # last counter already answered
pending_timer: Dict[int,asyncio.Task] = {}
current_reply_tasks: Dict[int,asyncio.Task] = {}

# ── utilities ────────────────────────────────────────────────────────────

def split_chunks(text:str):
    parts, buf = [], ""
    for ch in text:
        if ch in "?!":
            buf+=ch; parts.append(buf.strip()); buf=""
        elif ch in ".—\n":
            if buf.strip(): parts.append(buf.strip()); buf=""
        else:
            buf+=ch
    if buf.strip(): parts.append(buf.strip())
    return parts

async def send_chunks(ctx:ContextTypes.DEFAULT_TYPE, chat_id:int, text:str, counter:int):
    # initial “thinking” pause
    if msg_counter.get(chat_id)!=counter: return
    await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)
    await asyncio.sleep(random.uniform(1.0,2.0))
    for chunk in split_chunks(text):
        if msg_counter.get(chat_id)!=counter: return  # abort on new user msg
        speed = random.uniform(0.06, 0.10)           # seconds per char
        delay = max(1.0, min(6.0, len(chunk)*speed)) # clamp 1–6 s per chunk
        await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)
        await asyncio.sleep(delay)
        if msg_counter.get(chat_id)!=counter: return
        await ctx.bot.send_message(chat_id, chunk.lower())
        await asyncio.sleep(0.4)

async def update_long(phrase:str):
    p=phrase.strip()
    if p and p not in long_mem:
        long_mem.append(p)
        json.dump(long_mem,open(LONG_MEMORY_FILE,"w",encoding="utf-8"),ensure_ascii=False,indent=2)

# ── core generation (unchanged) ──────────────────────────────────────────
async def respond(chat_id:int, ctx:ContextTypes.DEFAULT_TYPE, counter:int):
    if msg_counter.get(chat_id)!=counter: return
    msgs=[{"role":"system","content":SYSTEM_PROMPT+" be concise."}]
    if USE_MEMORY and long_mem:
        msgs.append({"role":"system","content":"long-term memory: "+"; ".join(long_mem)})
    msgs+=short_mem
    try:
        llm = await asyncio.to_thread(lambda: client.chat.completions.create(
            model=FAYE_MODEL,
            messages=msgs,
            temperature=0.6,
            max_tokens=250))
    except OpenAIError as e:
        logging.error(e); return
    if msg_counter.get(chat_id)!=counter: return
    reply = re.sub(r"\s*—\s*", ", ", llm.choices[0].message.content.strip())
    reply = re.sub(r"\s{2,}", " ", reply)
    short_mem.append({"role":"assistant","content":reply})
    json.dump(short_mem,open(SHORT_MEMORY_FILE,"w",encoding="utf-8"),ensure_ascii=False,indent=2)

    if t := current_reply_tasks.get(chat_id): t.cancel()
    current_reply_tasks[chat_id] = asyncio.create_task(send_chunks(ctx,chat_id,reply,counter))

    if USE_MEMORY:
        try:
            mem = await asyncio.to_thread(lambda: client.chat.completions.create(
                model=MEMORY_MODEL, temperature=0.3, max_tokens=30,
                messages=[
                    {"role":"system","content":"extract fact or empty"},
                    {"role":"user","content":short_mem[-2]['content']}
                ]))
            await update_long(mem.choices[0].message.content)
        except OpenAIError as e:
            logging.error("mem extract "+str(e))

async def timer(chat_id:int, ctx:ContextTypes.DEFAULT_TYPE, counter:int):
    try:
        await asyncio.sleep(IDLE_SECONDS)
        await respond(chat_id, ctx, counter)
    except asyncio.CancelledError:
        return
    finally:
        pending_timer.pop(chat_id,None)

# ── handlers (unchanged) ────────────────────────────────────────────────
async def on_msg(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    chat_id, txt = update.effective_chat.id, update.message.text
    short_mem.append({"role":"user","content":txt})
    json.dump(short_mem,open(SHORT_MEMORY_FILE,"w",encoding="utf-8"),ensure_ascii=False,indent=2)

    counter = msg_counter[chat_id] = msg_counter.get(chat_id,0)+1
    if t := pending_timer.get(chat_id): t.cancel()
    if r := current_reply_tasks.get(chat_id): r.cancel()
    pending_timer[chat_id] = asyncio.create_task(timer(chat_id, ctx, counter))

async def clear(update:Update, ctx:ContextTypes.DEFAULT_TYPE):
    short_mem.clear(); json.dump(short_mem,open(SHORT_MEMORY_FILE,"w",encoding="utf-8"))
    await ctx.bot.send_message(update.effective_chat.id,"short‑term memory cleared")

async def on_err(update, ctx):
    if isinstance(ctx.error, Conflict): return
    logging.error(ctx.error)

# ── main ─────────────────────────────────────────────────────────────────
if __name__=="__main__":
    logging.basicConfig(level=logging.INFO)
    app=ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_error_handler(on_err)
    try:
        app.bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass
    app.add_handler(CommandHandler("clear_memory", clear))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_msg))
    print(f"Faye running ({FAYE_MODEL}) …")
    app.run_polling(drop_pending_updates=True)
