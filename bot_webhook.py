# bot_webhook.py — Telegram grup içi TODO botu (Google Sheets kalıcı depolama)
# Özellikler: /gorev ve /todo, /list, /clear, inline "✅ Tamamla"
# PTB webhook: python-telegram-bot[webhooks]==21.4
# Google Sheets: gspread + google-auth

import os, json, datetime, logging, base64
from typing import Dict, List, Optional

import gspread
from google.oauth2.service_account import Credentials

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes
)

# ---- Environment ----
TOKEN = os.getenv("TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")
PORT = int(os.getenv("PORT", "10000"))
HOST = "0.0.0.0"

GSHEET_ID = os.getenv("GSHEET_ID", "")
GSHEET_KEY_JSON = os.getenv("GSHEET_KEY_JSON", "")
GSHEET_KEY_B64 = os.getenv("GSHEET_KEY_B64", "")  # opsiyonel alternatif

# ---- Google Sheets helpers ----
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

def _load_sa_info() -> Dict:
    if GSHEET_KEY_JSON:
        return json.loads(GSHEET_KEY_JSON)
    if GSHEET_KEY_B64:
        data = base64.b64decode(GSHEET_KEY_B64).decode("utf-8")
        return json.loads(data)
    raise SystemExit("Google Sheets anahtarı bulunamadı. GSHEET_KEY_JSON veya GSHEET_KEY_B64 ekleyin.")

def _gc() -> gspread.Client:
    sa_info = _load_sa_info()
    creds = Credentials.from_service_account_info(sa_info, scopes=SCOPES)
    return gspread.authorize(creds)

def _ws():
    """tasks adlı çalışma sayfasını döndürür; yoksa oluşturur ve başlık yazar."""
    if not GSHEET_ID:
        raise SystemExit("GSHEET_ID env gerekli (Sheet URL'sindeki ID).")
    client = _gc()
    sh = client.open_by_key(GSHEET_ID)
    try:
        ws = sh.worksheet("tasks")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="tasks", rows=100, cols=6)
        ws.update("A1:F1", [["chat_id","message_id","task","done","by","ts"]])
    return ws

# ---- Data ops (Sheets) ----
def sheet_insert_task(chat_id: int, message_id: int, task: str):
    ws = _ws()
    # Append row
    ws.append_row([str(chat_id), str(message_id), task, "0", "", ""], value_input_option="USER_ENTERED")

def sheet_list_tasks(chat_id: int):
    """
    Sheets -> list of dicts (tipleri normalleştirerek) döndürür.
    - chat_id: int'e normalize edilir (float -> int vb.)
    - done: 1/true/evet gibi değerler True sayılır
    """
    ws = _ws()
    vals = ws.get_all_values()
    if not vals:
        return [], []

    headers = [h.strip() for h in vals[0]]
    rows = []
    for row in vals[1:]:
        # kısa satırlar için pad
        row = row + [""] * (len(headers) - len(row))
        rows.append({h: row[i] for i, h in enumerate(headers)})

    def to_int_like(v):
        try:
            return int(float(str(v).strip()))
        except Exception:
            return None

    target = to_int_like(chat_id)

    def is_done(v) -> bool:
        s = str(v).strip().lower()
        return s in ("1", "true", "evet", "yes", "x", "✓")

    open_tasks, done_tasks = [], []
    for r in rows:
        cid = to_int_like(r.get("chat_id", ""))
        if cid != target:
            continue
        if is_done(r.get("done", "0")):
            done_tasks.append(r)
        else:
            open_tasks.append(r)

    return open_tasks, done_tasks


def sheet_mark_done(chat_id: int, message_id: int, by: str, ts: str) -> Optional[str]:
    ws = _ws()
    vals = ws.get_all_values()
    # Header: chat_id, message_id, task, done, by, ts
    # Find row index (1-based)
    for idx in range(2, len(vals)+1):
        row = vals[idx-1]
        if len(row) < 2: 
            continue
        if row[0] == str(chat_id) and row[1] == str(message_id):
            # Update done/by/ts
            ws.update(f"D{idx}:F{idx}", [["1", by, ts]])
            return row[2] if len(row) > 2 else ""
    return None

def sheet_clear_chat(chat_id: int):
    ws = _ws()
    vals = ws.get_all_values()
    # Keep header + rows NOT matching chat_id
    kept = [vals[0]] if vals else [["chat_id","message_id","task","done","by","ts"]]
    for idx in range(2, len(vals)+1):
        row = vals[idx-1]
        if len(row) == 0:
            continue
        if row and row[0] != str(chat_id):
            kept.append(row)
    # Rewrite all
    ws.clear()
    ws.update(f"A1", kept)

# ---- UI helpers ----
def task_text(task: str, done: bool, by: str | None, ts: str | None) -> str:
    if done:
        meta = f"\n\n✅ <b>Tamamlandı</b> {ts} • {by}"
        return f"<s>{task}</s>{meta}"
    return f"📝 <b>Görev</b>\n{task}"

def kb(done: bool, chat_id: int, message_id: int) -> InlineKeyboardMarkup:
    if done:
        return InlineKeyboardMarkup([[InlineKeyboardButton("✅ Tamamlandı", callback_data="noop")]])
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ Tamamla", callback_data=f"done|{chat_id}|{message_id}")]])

# ---- Commands ----
async def gorev(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("Kullanım: /gorev <metin>")
        return
    sent = await update.message.reply_html(
        task_text(text, False, None, None),
        reply_markup=kb(False, update.effective_chat.id, 0)
    )
    # doğru message_id ile butonu güncelle
    await sent.edit_reply_markup(reply_markup=kb(False, update.effective_chat.id, sent.message_id))
    # Sheets'e yaz
    sheet_insert_task(update.effective_chat.id, sent.message_id, text)

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    open_tasks, done_tasks = sheet_list_tasks(update.effective_chat.id)
    if not open_tasks and not done_tasks:
        await update.message.reply_text("Şu an hiç görev yok.")
        return
    text = ""
    if open_tasks:
        text += "<b>🟢 Yapılacaklar</b>\n" + "\n".join("📝 "+r["task"] for r in open_tasks) + "\n\n"
    if done_tasks:
        text += "<b>⚪ Tamamlananlar</b>\n" + "\n".join(f"✅ {r['task']} — {r['by']} ({r['ts']})" for r in done_tasks)
    await update.message.reply_html(text)

async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sheet_clear_chat(update.effective_chat.id)
    await update.message.reply_text("📋 Görev listesi temizlendi.")

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
    ts = datetime.datetime.now().strftime("%d.%m.%Y %H:%M")

    task_title = sheet_mark_done(chat_id_i, msg_id_i, by, ts)
    if task_title is None:
        return

    await context.bot.edit_message_text(
        chat_id=chat_id_i, message_id=msg_id_i,
        text=task_text(task_title, True, by, ts),
        parse_mode=ParseMode.HTML,
        reply_markup=kb(True, chat_id_i, msg_id_i)
    )

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
    app.add_handler(CommandHandler("todo", gorev))  # eski alışkanlık için
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("clear", clear_cmd))
    app.add_handler(CallbackQueryHandler(button))

    # PTB kendi webhook sunucusunu çalıştırır
    app.run_webhook(
        listen=HOST,
        port=PORT,
        url_path="webhook",  # Telegram’a /webhook gönderiyoruz
        webhook_url=f"{PUBLIC_URL}/webhook",
        secret_token=WEBHOOK_SECRET or None,
    )

if __name__ == "__main__":
    main()

