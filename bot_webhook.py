# bot_webhook.py
# Telegram TODO botu (webhook modunda, grup içi)
# Özellikler: /gorev ve /todo, /list, /clear, inline "✅ Tamamla"
# python-telegram-bot==21.x  +  aiohttp web server

import os, json, datetime, logging, asyncio
from typing import Dict

from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    ContextTypes
)

# ====== ENV ======
TOKEN = os.getenv("TOKEN")                          # BotFather token (ENV'den)
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")    # Güvenlik için header secret (opsiyonel ama önerilir)
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")# Örn: https://telegram-todo-bot-webhook.onrender.com
PORT = int(os.getenv("PORT", "10000"))              # Render free rastgele port verebilir
HOST = "0.0.0.0"

# ====== Basit DB (JSON dosyası) ======
DATA_FILE = "tasks.json"

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

# ====== Komutlar ======
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

    sent = await update.message.reply_html(
        task_text(text, False, None, None),
        reply_markup=kb(False, update.effective_chat.id, 0) # placeholder
    )
    # message_id geldikten sonra doğru callback_data ile güncelle
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

    def fmt_open(r): return f"📝 {r['task']}"
    def fmt_done(r): return f"✅ {r['task']} — {r['by']} ({r['ts']})"

    text = ""
    if open_tasks:
        text += "<b>🟢 Yapılacaklar</b>\n" + "\n".join(map(fmt_open, open_tasks)) + "\n\n"
    if done_tasks:
        text += "<b>⚪ Tamamlananlar</b>\n" + "\n".join(map(fmt_done, done_tasks))

    await update.message.reply_html(text)

async def clear_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = load_db()
    chat_id = update.effective_chat.id
    keys = [k for k in db if k.startswith(str(chat_id))]
    if not keys:
        await update.message.reply_text("Zaten boş.")
        return
    for k in keys:
        del db[k]
    save_db(db)
    await update.message.reply_text("📋 Görev listesi temizlendi.")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    d = (q.data or "")
    if d == "noop":
        return
    if d.startswith("done|"):
        _, chat_id_str, msg_id_str = d.split("|", 2)
        chat_id, msg_id = int(chat_id_str), int(msg_id_str)

        db = load_db()
        key = task_key(chat_id, msg_id)
        rec = db.get(key)
        if not rec or rec["done"]:
            return

        user = q.from_user
        by = user.full_name or (user.username and f"@{user.username}") or "birisi"
        ts = datetime.datetime.now().strftime("%d.%m.%Y %H:%M")

        rec.update({"done": True, "by": by, "ts": ts})
        save_db(db)

        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=msg_id,
            text=task_text(rec["task"], True, by, ts),
            parse_mode=ParseMode.HTML,
            reply_markup=kb(True, chat_id, msg_id)
        )

# ====== Webhook Sunucusu ======
async def make_web_app(ptb_app: Application) -> web.Application:
    webapp = web.Application()

    async def health(_: web.Request) -> web.Response:
        return web.Response(text="ok")
    webapp.router.add_get("/", health)

    async def handle(request: web.Request) -> web.Response:
        # (Opsiyonel) secret token kontrolü
        if WEBHOOK_SECRET and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
            return web.Response(status=403, text="forbidden")

        data = await request.json()
        update = Update.de_json(data, ptb_app.bot)

        # PTB 21.x: process_update yerine update_queue'ya koyuyoruz
        await ptb_app.update_queue.put(update)
        return web.Response(text="ok")

    webapp.router.add_post("/webhook", handle)
    return webapp

async def main():
    if not TOKEN:
        raise SystemExit("TOKEN env değişkeni zorunlu.")
    if not PUBLIC_URL:
        raise SystemExit("PUBLIC_URL env değişkeni zorunlu (https://... )")

    logging.basicConfig(level=logging.INFO)

    # PTB Application
    ptb_app = ApplicationBuilder().token(TOKEN).build()
    ptb_app.add_handler(CommandHandler("start", start))
    ptb_app.add_handler(CommandHandler("help", help_cmd))
    ptb_app.add_handler(CommandHandler("gorev", _create_task))
    ptb_app.add_handler(CommandHandler("todo",  _create_task))
    ptb_app.add_handler(CommandHandler("list",  list_tasks))
    ptb_app.add_handler(CommandHandler("clear", clear_tasks))
    ptb_app.add_handler(CallbackQueryHandler(button_handler))

    # PTB initialize & start (webhook öncesi)
    await ptb_app.initialize()
    await ptb_app.start()

    # Webhook kur
    await ptb_app.bot.set_webhook(
        url=f"{PUBLIC_URL}/webhook",
        secret_token=WEBHOOK_SECRET or None
    )

    # Aiohttp server
    webapp = await make_web_app(ptb_app)
    runner = web.AppRunner(webapp)
    await runner.setup()
    site = web.TCPSite(runner, HOST, PORT)
    await site.start()
    logging.info("Webhook server started on %s:%s", HOST, PORT)

    # Sonsuz bekleme
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        # (Render stop vs.) düzgün kapat
        await ptb_app.stop()
        await ptb_app.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
