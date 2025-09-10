# bot_webhook.py (Webhook için sadeleştirilmiş)
import os, json, datetime, logging
from typing import Dict
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes
)

TOKEN = os.getenv("TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")
PORT = int(os.getenv("PORT", "10000"))
HOST = "0.0.0.0"

DATA_FILE = "tasks.json"

def load_db() -> Dict:
    if not os.path.exists(DATA_FILE): return {}
    with open(DATA_FILE, "r", encoding="utf-8") as f: return json.load(f)

def save_db(db: Dict): 
    with open(DATA_FILE, "w", encoding="utf-8") as f: json.dump(db, f, ensure_ascii=False, indent=2)

def task_key(chat_id: int, message_id: int) -> str: return f"{chat_id}:{message_id}"

def task_text(task: str, done: bool, by: str|None, ts: str|None) -> str:
    if done: return f"<s>{task}</s>\n\n✅ <b>Tamamlandı</b> {ts} • {by}"
    return f"📝 <b>Görev</b>\n{task}"

def kb(done: bool, chat_id: int, message_id: int):
    if done: return InlineKeyboardMarkup([[InlineKeyboardButton("✅ Tamamlandı", callback_data="noop")]])
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ Tamamla", callback_data=f"done|{chat_id}|{message_id}")]])

# ==== Commands ====
async def gorev(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args).strip()
    if not text: 
        await update.message.reply_text("Kullanım: /gorev <metin>")
        return
    sent = await update.message.reply_html(task_text(text, False, None, None),
                                           reply_markup=kb(False, update.effective_chat.id, 0))
    await sent.edit_reply_markup(reply_markup=kb(False, update.effective_chat.id, sent.message_id))
    db = load_db()
    db[task_key(update.effective_chat.id, sent.message_id)] = {"task": text,"done": False,"by": None,"ts": None}
    save_db(db)

async def list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db, chat_id = load_db(), update.effective_chat.id
    open_tasks, done_tasks = [], []
    for k,v in db.items():
        if k.startswith(str(chat_id)):
            (done_tasks if v["done"] else open_tasks).append(v)
    if not open_tasks and not done_tasks:
        await update.message.reply_text("Şu an hiç görev yok."); return
    text=""
    if open_tasks: text+="<b>🟢 Yapılacaklar</b>\n"+"\n".join("📝 "+r["task"] for r in open_tasks)+"\n\n"
    if done_tasks: text+="<b>⚪ Tamamlananlar</b>\n"+"\n".join(f"✅ {r['task']} — {r['by']} ({r['ts']})" for r in done_tasks)
    await update.message.reply_html(text)

async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db=load_db(); keys=[k for k in db if k.startswith(str(update.effective_chat.id))]
    if not keys: await update.message.reply_text("Zaten boş."); return
    for k in keys: del db[k]; save_db(db); await update.message.reply_text("📋 Görevler temizlendi.")

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer()
    if q.data=="noop": return
    _, chat_id, msg_id=q.data.split("|"); key=task_key(int(chat_id),int(msg_id))
    db=load_db(); rec=db.get(key); 
    if not rec or rec["done"]: return
    by=q.from_user.full_name or q.from_user.username or "birisi"
    ts=datetime.datetime.now().strftime("%d.%m.%Y %H:%M")
    rec.update({"done":True,"by":by,"ts":ts}); save_db(db)
    await context.bot.edit_message_text(chat_id=int(chat_id),message_id=int(msg_id),
        text=task_text(rec["task"],True,by,ts),parse_mode=ParseMode.HTML,
        reply_markup=kb(True,int(chat_id),int(msg_id)))

def main():
    if not TOKEN: raise SystemExit("TOKEN env gerekli")
    if not PUBLIC_URL: raise SystemExit("PUBLIC_URL gerekli")

    app=Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("gorev",gorev))
    app.add_handler(CommandHandler("todo",gorev))
    app.add_handler(CommandHandler("list",list_tasks))
    app.add_handler(CommandHandler("clear",clear))
    app.add_handler(CallbackQueryHandler(button))

    # PTB'nin kendi webhook runner'ını kullanıyoruz
    app.run_webhook(
        listen=HOST,
        port=PORT,
        secret_token=WEBHOOK_SECRET or None,
        webhook_url=f"{PUBLIC_URL}/webhook"
    )

if __name__=="__main__":
    main()
