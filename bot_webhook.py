# Webhook’lu Telegram TODO botu (grup içi)
# Özellikler: /gorev ve /todo, /list, /clear, inline "✅ Tamamla"
# python-telegram-bot 21.x + aiohttp web server

import os, json, datetime, logging, asyncio
from typing import Dict
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    ContextTypes
)

TOKEN = os.getenv("TOKEN")                       # BotFather token
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "") # Key (opsiyonel ama iyi olur)
PORT = int(os.getenv("PORT", "10000"))           # Render free'de rastgele port verilebilir
HOST = "0.0.0.0"

DATA_FILE = "tasks.json"

# ---------- Basit kalıcı kayıt (JSON dosyası) ----------
def load_db() -> Dict:
    if not os.path.exists(DATA_FILE):
        return {}
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_db(db: Dict):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)

def task_key(chat_id: int, message_id: int) -> str:
    return f"{chat_id}:{message_id}"

def task_text(task: str, done: bool, by: str | None, ts: str | None) -> str:
    if done:
        meta = f"\n\n✅ <b>Tamamlandı</b> {ts} • {by}"
        return f"<s>{task}</s>{meta}"
    else:
        return f"📝 <b>Görev</b>\n{task}"

def kb(done: bool, chat_id: int, message_id: int) -> InlineKeyboardMarkup:
    if done:
        return InlineKeyboardMarkup([[InlineKeyboardButton("✅ Tamamlandı", callback_data="noop")]])
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("✅ Tamamla", callback_data=f"done|{chat_id}|{message_id}")]]
    )

# ---------- Komutlar ----------
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(
        "<b>Robot (webhook)</b>\n"
        "• /gorev <metin> veya /todo <metin>\n"
        "• /list — yapılacaklar / tamamlananlar\n"
        "• /clear — bu grubun görevlerini temizle"
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await help_cmd(update, context)

async def _create_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in ("group", "supergroup"):
        await update.message.reply_text("Bu komutu bir grupta kullanın.")
        return
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("Kullanım: /gorev <metin>")
        return

    sent = await update.message.reply_html(task_text(text, False, None, None),
                                           reply_markup=kb(False, update.effective_chat.id, 0))
    await sent.edit_reply_markup(reply_markup=kb(False, update.effective_chat.id, sent.message_id))

    db = load_db()
    db[task_key(update.effective_chat.id, sent.message_id)] = {
        "task": text, "done": False, "by": None, "ts": None
    }
    save_db(db)

async def list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = load_db()
    chat_id = update.effective_chat.id
    open_tasks, done_tasks = [], []
    for key, rec in db.items():
        if key.startswith(str(chat_id)):
            (done_tasks if rec["done"] else open_tasks).append(rec)
    if not open_tasks and not done_tasks:
        await update.message.reply_text("Şu an hiç görev yok.")
        return
    def fo(r): return f"📝 {r['task']}"
    def fd(r): return f"✅ {r['task']} — {r['by']} ({r['ts']})"
    text = ""
    if open_tasks: text += "<b>🟢 Yapılacaklar</b>\n" + "\n".join(map(fo, open_tasks)) + "\n\n"
    if done_tasks: text += "<b>⚪ Tamamlananlar</b>\n" + "\n".join(map(fd, done_tasks))
    await update.message.reply_html(text)

async def clear_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = load_db()
    chat_id = update.effective_chat.id
    keys = [k for k in db if k.startswith(str(chat_id))]
    if not keys:
        await update.message.reply_text("Zaten boş.")
        return
    for k in keys: del db[k]
    save_db(db)
    await update.message.reply_text("📋 Görev listesi temizlendi.")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    d = (q.data or "")
    if d == "noop": return
    if d.startswith("done|"):
        _, chat_id_str, msg_id_str = d.split("|", 2)
        chat_id, msg_id = int(chat_id_str), int(msg_id_str)
        db = load_db()
        key = task_key(chat_id, msg_id); rec = db.get(key)
        if not rec or rec["done"]: return
        user = q.from_user
        by = user.full_name or (user.username and f"@{user.username}") or "birisi"
        ts = datetime.datetime.now().strftime("%d.%m.%Y %H:%M")
        rec.update({"done": True, "by": by, "ts": ts}); save_db(db)
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=msg_id,
            text=task_text(rec["task"], True, by, ts),
            parse_mode=ParseMode.HTML, reply_markup=kb(True, chat_id, msg_id)
        )

# ---------- Webhook sunucusu ----------
async def main():
    if not TOKEN:
        raise SystemExit("TOKEN env değişkeni zorunlu.")
    logging.basicConfig(level=logging.INFO)
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("gorev", _create_task))
    app.add_handler(CommandHandler("todo",  _create_task))
    app.add_handler(CommandHandler("list",  list_tasks))
    app.add_handler(CommandHandler("clear", clear_tasks))
    app.add_handler(CallbackQueryHandler(button_handler))

    # Aiohttp web app
    webapp = web.Application()

    async def handle(request: web.Request) -> web.Response:
        if WEBHOOK_SECRET and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
            return web.Response(status=403, text="forbidden")
        data = await request.json()
        update = Update.de_json(data, app.bot)
        await app.process_update(update)
        return web.Response(text="ok")

    webapp.router.add_post("/webhook", handle)

    # PTB'nin set_webhook çağrısı
    public_url = os.getenv("PUBLIC_URL", "").rstrip("/")
    if not public_url:
        raise SystemExit("PUBLIC_URL env değişkenini Render URL’inle ayarla (https://... )")
    await app.bot.set_webhook(url=f"{public_url}/webhook",
                              secret_token=WEBHOOK_SECRET or None)

    runner = web.AppRunner(webapp); await runner.setup()
    site = web.TCPSite(runner, HOST, PORT); await site.start()
    logging.info("Webhook server started on %s:%s", HOST, PORT)
    # Sonsuza dek bekle
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
