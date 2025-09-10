# bot_webhook.py — Telegram TODO botu
# Özellikler:
#  - /gorev <metin>  → metnin sonuna otomatik oluşturulma zamanı ekler
#  - ✅ butonuyla tamamlama
#  - /list           → yapılacaklar & tamamlananlar
#  - /clear          → SADECE tamamlananları siler (yapılacakları korur)
#  - /inbox (HTTP POST) → WhatsApp/Zapier/Make tetiklerinden görev düşürür
#
# Google Sheet: 'tasks' sayfasında başlıklar:
#   chat_id | message_id | task | done | by | ts | owner | due | prio | created

import os, json, base64, logging
from typing import Dict, List, Optional
from datetime import datetime, timedelta

import gspread
from google.oauth2.service_account import Credentials
from aiohttp import web

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes
)

# ---------- ENV ----------
TOKEN = os.getenv("TOKEN", "")
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
PORT = int(os.getenv("PORT", "10000"))
HOST = "0.0.0.0"

GSHEET_ID = os.getenv("GSHEET_ID", "")
GSHEET_KEY_JSON = os.getenv("GSHEET_KEY_JSON", "")
GSHEET_KEY_B64 = os.getenv("GSHEET_KEY_B64", "")

# WhatsApp/Zapier/Make için özel endpoint
INBOX_SECRET = os.getenv("INBOX_SECRET", "")     # /inbox çağrılarında X-Secret ile gelecek
DEFAULT_CHAT_ID = os.getenv("DEFAULT_CHAT_ID", "")  # opsiyonel: görevlerin düşeceği varsayılan grup id

# ---------- Saat/Tarih ----------
TZ_OFFSET = 3  # İstanbul için basit ofset

def _now():
    return datetime.utcnow() + timedelta(hours=TZ_OFFSET)

def created_now_str() -> str:
    return _now().strftime("%d.%m.%Y %H:%M")

# ---------- Google Sheets helpers ----------
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

def _load_sa_info() -> Dict:
    if GSHEET_KEY_JSON:
        return json.loads(GSHEET_KEY_JSON)
    if GSHEET_KEY_B64:
        return json.loads(base64.b64decode(GSHEET_KEY_B64).decode("utf-8"))
    raise SystemExit("Google Sheets anahtarı yok. GSHEET_KEY_JSON veya GSHEET_KEY_B64 ekleyin.")

def _gc() -> gspread.Client:
    creds = Credentials.from_service_account_info(_load_sa_info(), scopes=SCOPES)
    return gspread.authorize(creds)

def _ws():
    """tasks sayfasını döndür; yoksa oluştur ve başlıkları yaz."""
    if not GSHEET_ID:
        raise SystemExit("GSHEET_ID env gerekli.")
    sh = _gc().open_by_key(GSHEET_ID)
    try:
        ws = sh.worksheet("tasks")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="tasks", rows=200, cols=10)
        ws.update("A1:J1", [[
            "chat_id","message_id","task","done","by","ts","owner","due","prio","created"
        ]])
    return ws

def _headers(ws) -> List[str]:
    vals = ws.get_all_values()
    return [h.strip() for h in (vals[0] if vals else [])]

def _col_idx(headers: List[str], name: str) -> int:
    """1-based column index; yoksa 0 döner."""
    name = name.strip().lower()
    for i, h in enumerate(headers, start=1):
        if h.strip().lower() == name:
            return i
    return 0

# ---------- DB (Sheets) ----------
def sheet_insert_task(chat_id: int, message_id: int, task: str,
                      owner: Optional[str], due: Optional[str], prio: Optional[str],
                      created: str):
    ws = _ws()
    ws.append_row(
        [str(chat_id), str(message_id), task, "0", "", "", owner or "", due or "", prio or "", created],
        value_input_option="USER_ENTERED"
    )

def sheet_list_tasks(chat_id: int):
    """Tipleri normalize ederek bu chat'e ait açık/tamam görevleri döndürür."""
    ws = _ws()
    vals = ws.get_all_values()
    if not vals:
        return [], []
    headers = [h.strip() for h in vals[0]]

    def row_to_dict(row):
        row = row + [""] * (len(headers) - len(row))
        return {headers[i]: row[i] for i in range(len(headers))}

    def to_int_like(v):
        try: return int(float(str(v).strip()))
        except: return None

    def is_done(v) -> bool:
        s = str(v).strip().lower()
        return s in ("1","true","evet","yes","x","✓")

    target = to_int_like(chat_id)
    open_tasks, done_tasks = [], []
    for rvals in vals[1:]:
        r = row_to_dict(rvals)
        if to_int_like(r.get("chat_id","")) != target:
            continue
        if is_done(r.get("done","0")):
            done_tasks.append(r)
        else:
            open_tasks.append(r)
    return open_tasks, done_tasks

def sheet_mark_done(chat_id: int, message_id: int, by: str, ts: str) -> Optional[str]:
    ws = _ws()
    vals = ws.get_all_values()
    if not vals:
        return None
    headers = [h.strip() for h in vals[0]]
    c_chat   = _col_idx(headers, "chat_id")
    c_msg    = _col_idx(headers, "message_id")
    c_task   = _col_idx(headers, "task")
    c_done   = _col_idx(headers, "done")
    c_by     = _col_idx(headers, "by")
    c_ts     = _col_idx(headers, "ts")

    if not all([c_chat, c_msg, c_task, c_done, c_by, c_ts]):
        return None

    for idx in range(2, len(vals)+1):
        row = vals[idx-1]
        def val(col): return (row[col-1] if len(row) >= col else "").strip()
        try:
            cid = int(float(val(c_chat)))
            mid = int(float(val(c_msg)))
        except:
            continue
        if cid == chat_id and mid == message_id:
            task_text = val(c_task)
            ws.update_cell(idx, c_done, "1")
            ws.update_cell(idx, c_by, by)
            ws.update_cell(idx, c_ts, ts)
            return task_text
    return None

def sheet_clear_done(chat_id: int):
    """Bu chat için SADECE tamamlanan görevleri siler."""
    ws = _ws()
    vals = ws.get_all_values()
    if not vals:
        return
    headers = vals[0]
    kept = [headers]

    c_chat = _col_idx(headers, "chat_id")
    c_done = _col_idx(headers, "done")

    for row in vals[1:]:
        try:
            cid = int(float(row[c_chat-1]))
        except:
            cid = None
        done_flag = row[c_done-1].strip().lower() in ("1","true","evet","yes","x","✓")

        # aynı chat'e ait ve tamamlanmışsa sil; diğerlerini koru
        if cid == int(chat_id) and done_flag:
            continue
        kept.append(row)

    ws.clear()
    ws.update("A1", kept)

# ---------- UI ----------
def task_text(task: str, done: bool, by: Optional[str], ts: Optional[str]) -> str:
    if done:
        meta = f"\n\n✅ <b>Tamamlandı</b> {ts} • {by}"
        return f"<s>{task}</s>{meta}"
    return f"📝 <b>Görev</b>\n{task}"

def kb(done: bool, chat_id: int, message_id: int) -> InlineKeyboardMarkup:
    if done:
        return InlineKeyboardMarkup([[InlineKeyboardButton("✅ Tamamlandı", callback_data="noop")]])
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ Tamamla", callback_data=f"done|{chat_id}|{message_id}")]])

# ---------- Telegram Handlers ----------
async def gorev(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = " ".join(context.args).strip()
    if not raw:
        await update.message.reply_text("Kullanım: /gorev <metin>")
        return

    # Görev metnine oluşturulma zamanını ekle
    title_with_created = f"{raw} {created_now_str()}"

    sent = await update.message.reply_html(
        task_text(title_with_created, False, None, None),
        reply_markup=kb(False, update.effective_chat.id, 0)
    )
    await sent.edit_reply_markup(reply_markup=kb(False, update.effective_chat.id, sent.message_id))

    # Sheets'e kaydet (owner/due/prio şimdilik boş)
    sheet_insert_task(
        update.effective_chat.id,
        sent.message_id,
        title_with_created,
        owner=None, due=None, prio=None,
        created=created_now_str()
    )

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    open_tasks, done_tasks = sheet_list_tasks(update.effective_chat.id)
    if not open_tasks and not done_tasks:
        await update.message.reply_text("Şu an hiç görev yok.")
        return

    def line_open(r):
        created = r.get("created","")
        suffix = f" {created}" if created and created not in r.get("task","") else ""
        return f"📝 {r.get('task','')}{suffix}"

    def line_done(r):
        return f"✅ {r.get('task','')} — {r.get('by','')} ({r.get('ts','')})"

    text = ""
    if open_tasks:
        text += "<b>🟢 Yapılacaklar</b>\n" + "\n".join(map(line_open, open_tasks)) + "\n\n"
    if done_tasks:
        text += "<b>⚪ Tamamlananlar</b>\n" + "\n".join(map(line_done, done_tasks))

    await update.message.reply_html(text)

async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sheet_clear_done(update.effective_chat.id)
    await update.message.reply_text("🧹 Tamamlanan görevler silindi. Yapılacaklar duruyor.")

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "noop":
        return
    try:
        _, chat_id, msg_id = q.data.split("|", 2)
        chat_id_i, msg_id_i = int(chat_id), int(msg_id)
    except Exception:
        return

    by = q.from_user.full_name or (q.from_user.username and f"@{q.from_user.username}") or "birisi"
    ts = _now().strftime("%d.%m.%Y %H:%M")

    task_title = sheet_mark_done(chat_id_i, msg_id_i, by, ts)
    if task_title is None:
        return

    await context.bot.edit_message_text(
        chat_id=chat_id_i, message_id=msg_id_i,
        text=task_text(task_title, True, by, ts),
        parse_mode=ParseMode.HTML,
        reply_markup=kb(True, chat_id_i, msg_id_i)
    )

# ---------- HTTP /inbox (WhatsApp/Zapier/Make) ----------
async def inbox_handler(request: web.Request):
    # Güvenlik kontrolü (shared secret)
    secret = request.headers.get("X-Secret", "")
    if not INBOX_SECRET or secret != INBOX_SECRET:
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid_json"}, status=400)

    text = str(payload.get("text", "")).strip()
    customer = str(payload.get("customer", "")).strip()
    phone = str(payload.get("phone", "")).strip()
    chat_id_override = payload.get("chat_id", None)

    if not text:
        return web.json_response({"ok": False, "error": "missing text"}, status=400)

    # Görev başlığı
    parts = [text]
    meta = []
    if customer: meta.append(customer)
    if phone:    meta.append(phone)
    if meta:
        parts.append("— " + " • ".join(meta))
    parts.append(created_now_str())
    title = " ".join(parts)

    # Hedef chat
    target_env = (chat_id_override if chat_id_override is not None else DEFAULT_CHAT_ID).strip() if isinstance(DEFAULT_CHAT_ID, str) else chat_id_override
    try:
        target_chat_id = int(target_env)
    except Exception:
        return web.json_response({"ok": False, "error": "no chat_id (set DEFAULT_CHAT_ID env or pass chat_id)"},
                                 status=400)

    # Telegram'a mesaj + buton
    ptb_app: Application = request.app["ptb_app"]
    sent = await ptb_app.bot.send_message(
        chat_id=target_chat_id,
        text=task_text(title, False, None, None),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Tamamla", callback_data=f"done|{target_chat_id}|0")]])
    )
    # doğru message_id ile inline keyboard’u güncelle
    await ptb_app.bot.edit_message_reply_markup(
        chat_id=target_chat_id,
        message_id=sent.message_id,
        reply_markup=kb(False, target_chat_id, sent.message_id)
    )

    # Sheets’e yaz
    sheet_insert_task(
        target_chat_id,
        sent.message_id,
        title,
        owner=None, due=None, prio=None,
        created=created_now_str()
    )

    return web.json_response({"ok": True, "message_id": sent.message_id})

# ---------- main ----------
def main():
    if not TOKEN:
        raise SystemExit("TOKEN env gerekli")
    if not PUBLIC_URL:
        raise SystemExit("PUBLIC_URL env gerekli (https://...)")
    if not GSHEET_ID:
        raise SystemExit("GSHEET_ID env gerekli")
    if not (GSHEET_KEY_JSON or GSHEET_KEY_B64):
        raise SystemExit("GSHEET_KEY_JSON veya GSHEET_KEY_B64 env gerekli")

    logging.basicConfig(level=logging.INFO)

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("gorev", gorev))
    app.add_handler(CommandHandler("todo", gorev))  # eski alışkanlık için kısayol
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("clear", clear_cmd))
    app.add_handler(CallbackQueryHandler(button))

    # Aiohttp uygulaması: /inbox endpoint’i
    web_app = web.Application()
    web_app["ptb_app"] = app
    web_app.router.add_post("/inbox", inbox_handler)

    # PTB webhook (Telegram) + bizim web_app birlikte
    app.run_webhook(
        listen=HOST,
        port=PORT,
        url_path="webhook",
        webhook_url=f"{PUBLIC_URL}/webhook",
        secret_token=WEBHOOK_SECRET or None,
        web_app=web_app,  # <- bizim /inbox burada çalışıyor
    )

if __name__ == "__main__":
    main()
