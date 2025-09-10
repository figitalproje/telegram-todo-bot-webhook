# -*- coding: utf-8 -*-
# Telegram grup İŞİ/TODO botu + Webhook /inbox entegrasyonu (PTB 21.x)
# Özellikler: /gorev, Tamamla butonu, /list, /clear(yalnızca tamamlananları siler), /inbox HTTP
# Opsiyonel: Google Sheets’e yaz (GSHEET_ID + GOOGLE_APPLICATION_CREDENTIALS varsa)

import os, json, datetime, logging, asyncio
from typing import Dict, Tuple, Optional

from aiohttp import web

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ---- Logging ----
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("todo-bot")

# ---- ENV ----
TOKEN = os.getenv("TOKEN", "")
PUBLIC_URL = os.getenv("PUBLIC_URL", "")
INBOX_SECRET = os.getenv("INBOX_SECRET", "")
DEFAULT_CHAT_ID = int(os.getenv("DEFAULT_CHAT_ID", "0"))

GSHEET_ID = os.getenv("GSHEET_ID", "")
GOOGLE_APPLICATION_CREDENTIALS = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")

# ---- Basit JSON DB ----
DATA_FILE = "tasks.json"


def load_db() -> Dict:
    if not os.path.exists(DATA_FILE):
        return {}
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_db(db: Dict) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)


def key_for(chat_id: int, msg_id: int) -> str:
    return f"{chat_id}:{msg_id}"


# ---- Metin/Buton yardımcıları ----
def task_text(title: str, done: bool, by: Optional[str], ts: Optional[str]) -> str:
    """Görev mesajı gövdesi (HTML)."""
    if done:
        meta = f"✅ Tamamlandı — {ts} · {by}"
        return f"<b>✅ Tamamlananlar</b>\n<code>{title}</code>\n<i>{meta}</i>"
    else:
        return f"<b>🟢 Yapılacaklar</b>\n<code>{title}</code>"


def keyboard(done: bool, chat_id: int, msg_id: int) -> InlineKeyboardMarkup:
    if done:
        # İstersen burada 'Geri Al' butonu da koyabilirsin; şimdilik kapalı.
        return InlineKeyboardMarkup.from_row(
            [InlineKeyboardButton("↩️ Geri Al (yakında)", callback_data="noop")]
        )
    else:
        return InlineKeyboardMarkup.from_row(
            [InlineKeyboardButton("✅ Tamamla", callback_data=f"done|{chat_id}|{msg_id}")]
        )


def now_str() -> str:
    return datetime.datetime.now().strftime("%d.%m.%Y %H:%M")


def user_name(u) -> str:
    return u.full_name or (u.username and f"@{u.username}") or "birisi"


def make_title_with_ts(raw: str) -> str:
    base = raw.strip()
    ts = now_str()
    return f"{base} — {ts}"


# ---- Google Sheets (opsiyonel, varsa çalışır) ----
def _sheet_client_or_none():
    if not GSHEET_ID or not GOOGLE_APPLICATION_CREDENTIALS:
        return None
    try:
        import gspread
        # service account dosya yolu env’de verilmeli
        gc = gspread.service_account(filename=GOOGLE_APPLICATION_CREDENTIALS)
        sh = gc.open_by_key(GSHEET_ID)
        try:
            ws = sh.worksheet("Tasks")
        except Exception:
            ws = sh.add_worksheet(title="Tasks", rows=1000, cols=10)
            ws.append_row(["chat_id", "message_id", "title", "status", "by", "when"], value_input_option="RAW")
        return ws
    except Exception:
        log.exception("Google Sheets bağlantı hatası")
        return None


def sheet_append(chat_id: int, msg_id: int, title: str, by: str, when: str, status: str = "New"):
    ws = _sheet_client_or_none()
    if not ws:
        return
    try:
        ws.append_row([chat_id, msg_id, title, status, by, when], value_input_option="RAW")
    except Exception:
        log.exception("sheet_append hata")


def sheet_mark_done(chat_id: int, msg_id: int, by: str, when: str):
    ws = _sheet_client_or_none()
    if not ws:
        return
    try:
        # message_id eşleşen satırı bulup status/by/when güncelleyelim
        cells = ws.col_values(2)  # message_id kolonu
        for idx, v in enumerate(cells, start=1):
            if str(v).strip() == str(msg_id):
                ws.update_cell(idx, 4, "Done")  # status
                ws.update_cell(idx, 5, by)      # by
                ws.update_cell(idx, 6, when)    # when
                break
    except Exception:
        log.exception("sheet_mark_done hata")


# ---- /inbox için yardımcı ----
def create_task_message_for_inbox(chat_id: int, raw_text: str, by: str = "Webhook") -> Tuple[str, InlineKeyboardMarkup]:
    title = raw_text.strip()
    if not title.lower().startswith(("siparis", "sipariş")):
        title = f"SİPARİŞ: {title}"
    title = make_title_with_ts(title)
    txt = task_text(title, done=False, by=None, ts=None)
    kb = keyboard(False, chat_id, 0)  # msg_id burada placeholder; edit ederken gerekmiyor
    return txt, kb


# ---- Telegram handlers ----
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(
        "Merhaba! 👋\n"
        "Yeni görev: <code>/gorev Görev başlığı</code>\n"
        "Liste: <code>/list</code>\n"
        "Temizle (yalnızca tamamlananlar): <code>/clear</code>"
    )


async def cmd_gorev(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Kullanım: /gorev Görev başlığı")
        return
    chat_id = update.effective_chat.id
    title = " ".join(context.args).strip()
    title = make_title_with_ts(title)

    txt = task_text(title, done=False, by=None, ts=None)
    sent = await update.message.reply_html(txt, reply_markup=keyboard(False, chat_id, update.message.id))
    # DB’ye yaz
    db = load_db()
    db[key_for(chat_id, sent.message_id)] = {
        "title": title,
        "done": False,
        "by": None,
        "ts": None,
    }
    save_db(db)

    # Sheets
    sheet_append(chat_id, sent.message_id, title, by=user_name(update.effective_user), when=now_str(), status="New")


async def cb_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        _, ch, mid = q.data.split("|", 2)
        chat_id, msg_id = int(ch), int(mid)
    except Exception:
        return

    db = load_db()
    rec = db.get(key_for(chat_id, msg_id))
    if not rec:
        # eski kayıt yoksa sadece görsel güncelle
        rec = {"title": "(başlık bulunamadı)", "done": True, "by": user_name(q.from_user), "ts": now_str()}
    else:
        rec["done"] = True
        rec["by"] = user_name(q.from_user)
        rec["ts"] = now_str()
    save_db(db)

    await context.bot.edit_message_text(
        chat_id=chat_id,
        message_id=msg_id,
        text=task_text(rec["title"], done=True, by=rec["by"], ts=rec["ts"]),
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard(True, chat_id, msg_id),
    )

    # Sheets
    sheet_mark_done(chat_id, msg_id, by=rec["by"], when=rec["ts"])


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    db = load_db()

    todos, dones = [], []
    for k, v in db.items():
        try:
            ch, _ = k.split(":")
            if int(ch) != chat_id:
                continue
        except Exception:
            continue
        if v.get("done"):
            meta = f"{v.get('by') or ''} — {v.get('ts') or ''}".strip(" —")
            dones.append(f"{v.get('title')}  ({meta})" if meta else v.get("title"))
        else:
            todos.append(v.get("title"))

    lines = []
    lines.append("🟢 <b>Yapılacaklar</b>")
    lines.extend([f"• {t}" for t in todos]) if todos else lines.append("— yok —")
    lines.append("")
    lines.append("✅ <b>Tamamlananlar</b>")
    lines.extend([f"• {d}" for d in dones]) if dones else lines.append("— yok —")

    await update.message.reply_html("\n".join(lines))


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sadece tamamlanan kayıtları siler."""
    chat_id = update.effective_chat.id
    db = load_db()
    before = len(db)
    keep = {}
    for k, v in db.items():
        try:
            ch, _ = k.split(":")
            if int(ch) != chat_id:
                keep[k] = v
                continue
        except Exception:
            keep[k] = v
            continue
        if not v.get("done"):  # yapılacak olanları koru
            keep[k] = v
    save_db(keep)
    deleted = before - len(keep)
    await update.message.reply_text(f"✅ {deleted} tamamlanmış görev temizlendi.")


# ---- AIOHTTP /inbox route ----
# Bu route’u, PTB’nin webhook sunucusuna (aynı uygulama) ekleyeceğiz.
routes = web.RouteTableDef()
_app_for_routes: Optional[Application] = None  # run_webhook sonrası atanır


@routes.post("/inbox")
async def inbox(request: web.Request):
    # Güvenlik
    secret = request.headers.get("X-Secret")
    if not secret or secret != INBOX_SECRET:
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid_json"}, status=400)

    raw_text = (body.get("text") or "").strip()
    if not raw_text:
        return web.json_response({"ok": False, "error": "text_required"}, status=400)

    chat_id = int(body.get("chat_id") or DEFAULT_CHAT_ID or 0)
    if not chat_id:
        return web.json_response({"ok": False, "error": "chat_id_required"}, status=400)

    # Mesajı görev olarak gönder
    txt, kb = create_task_message_for_inbox(chat_id, raw_text, by="Webhook")
    out = await _app_for_routes.bot.send_message(
        chat_id=chat_id,
        text=txt,
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
        disable_web_page_preview=True,
    )

    # DB’ye yaz
    db = load_db()
    db[key_for(chat_id, out.message_id)] = {
        "title": raw_text if raw_text else "(sipariş)",
        "done": False,
        "by": None,
        "ts": None,
    }
    save_db(db)

    # Sheets (opsiyonel)
    try:
        sheet_append(chat_id, out.message_id, raw_text, by="Webhook", when=now_str(), status="New")
    except Exception:
        log.exception("sheet_append (inbox) failed")

    return web.json_response({"ok": True, "message_id": out.message_id})


# ---- PTB init & webhook ----
async def post_init(application: Application) -> None:
    """run_webhook sırasında, PTB'nin kendi web_app'ine /inbox route'unu ekliyoruz."""
    global _app_for_routes
    _app_for_routes = application
    # PTB 21.x: application.web_app özelliği run_webhook içinde oluşturulur, post_init’te erişilebilir.
    application.web_app.add_routes(routes)
    log.info("Custom route '/inbox' eklendi.")


def main():
    if not TOKEN:
        raise SystemExit("TOKEN env gerekli")
    if not PUBLIC_URL:
        raise SystemExit("PUBLIC_URL env gerekli (https://...)" )
    if not INBOX_SECRET:
        log.warning("INBOX_SECRET tanımlı değil; /inbox güvenliği yok!")

    app: Application = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(post_init)  # <- /inbox route burada eklenir
        .build()
    )

    # Komutlar
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("gorev", cmd_gorev))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CallbackQueryHandler(cb_done, pattern=r"^done\|"))
    app.add_handler(CallbackQueryHandler(lambda u, c: u.callback_query.answer(), pattern=r"^noop$"))
    # (İstemezseniz serbest mesaj yakalama kapalı)

    # Webhook’u PTB üzerinden ayağa kaldır
    # Render tek port açar; PTB kendi aiohttp web sunucusunu başlatır ve biz de post_init’te /inbox ekledik.
    app.run_webhook(
        listen="0.0.0.0",
        port=int(os.getenv("PORT", "10000")),
        url_path="",  # kök
        webhook_url=f"{PUBLIC_URL}",
        secret_token=None,  # Telegram secret token kullanmıyoruz
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
