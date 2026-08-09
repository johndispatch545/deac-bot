"""
DEAC Bot — Stage 3 (Stage 1 dispatch + Stage 2 PTI/maintenance, merged
into a single bot/process/deployment).

Environment variables required:
  BOT_TOKEN          - Telegram bot token (one bot now, not two)
  SERVICE_GROUP_ID   - chat_id of the main dispatch/service group
"""
import json
import logging
import os
from datetime import datetime, timedelta, time as dtime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import storage
from keep_alive import start_keep_alive

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
log = logging.getLogger("deac_bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
SERVICE_GROUP_ID = int(os.environ["SERVICE_GROUP_ID"])
REMINDER_MINUTES = int(os.environ.get("REMINDER_MINUTES", "5"))
DAILY_REPORT_HOUR_UTC = int(os.environ.get("DAILY_REPORT_HOUR_UTC", "18"))

AWAITING_TRAILER = set()  # group_ids currently waiting for a trailer number reply
LAST_MEDIA = {}  # chat_id -> {"kind": "photo|document", "ref": str, "ts": datetime}
LAST_MEDIA_TTL_MINUTES = 10


def driver_label(driver):
    return driver.get("name") or "Unknown driver"


def progress_bar(done, total, width=12):
    if total == 0:
        return "—"
    filled = round(width * done / total)
    return "▓" * filled + "░" * (width - filled) + f"  {done}/{total}"


# ===========================================================================
# DISPATCH (Stage 1) — /register /BOL /t /POD /PU /DEL /done /pending /export
# ===========================================================================
async def cmd_register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /register First Last")
        return
    name = " ".join(context.args)
    data = storage.load()
    storage.ensure_driver(data, update.effective_chat.id, name=name)
    storage.save(data)
    await update.message.reply_text(f"✅ Registered: {name}")


async def post_or_edit_service_message(context, data, group_id, text):
    driver = storage.ensure_driver(data, group_id)
    pending = driver.get("pending")

    if pending and not pending.get("resolved", False):
        try:
            await context.bot.edit_message_text(
                chat_id=SERVICE_GROUP_ID, message_id=pending["service_msg_id"], text=text
            )
            pending["ts"] = storage.timestamp()
            pending["last_reminder_ts"] = storage.timestamp()
            storage.save(data)
            return
        except Exception as e:
            log.warning("Edit failed, sending new message instead: %s", e)

    msg = await context.bot.send_message(chat_id=SERVICE_GROUP_ID, text=text)
    driver["pending"] = {
        "service_msg_id": msg.message_id,
        "ts": storage.timestamp(),
        "last_reminder_ts": storage.timestamp(),
        "resolved": False,
    }
    storage.save(data)


async def cache_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remembers the last photo/document sent in a chat, even without a
    caption, so a separately-sent /BOL can still pick it up."""
    msg = update.message
    if msg.photo:
        LAST_MEDIA[update.effective_chat.id] = {
            "kind": "photo", "ref": msg.photo[-1].file_id, "ts": datetime.utcnow()
        }
    elif msg.document:
        LAST_MEDIA[update.effective_chat.id] = {
            "kind": "document", "ref": msg.document.file_id, "ts": datetime.utcnow()
        }


async def cmd_bol(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = storage.load()
    driver = storage.ensure_driver(data, update.effective_chat.id)

    msg = update.message
    ref, kind = None, None
    if msg.photo:
        ref, kind = msg.photo[-1].file_id, "photo"
    elif msg.document:
        ref, kind = msg.document.file_id, "document"
    elif context.args:
        ref, kind = " ".join(context.args), "link"
    else:
        # fallback: a photo/PDF sent as its own message just before /BOL
        cached = LAST_MEDIA.get(update.effective_chat.id)
        if cached and (datetime.utcnow() - cached["ts"]) <= timedelta(minutes=LAST_MEDIA_TTL_MINUTES):
            ref, kind = cached["ref"], cached["kind"]

    if not ref:
        await msg.reply_text(
            "Please attach a photo/PDF with caption /BOL, send /BOL <link>, "
            "or send the photo first and then /BOL right after."
        )
        return

    driver["active_bol"] = {"kind": kind, "ref": ref, "ts": storage.timestamp()}
    driver["last_status"] = None
    storage.save(data)
    await msg.reply_text("✅ BOL saved.")


async def cmd_t(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = storage.load()
    driver = storage.ensure_driver(data, update.effective_chat.id)
    name = driver_label(driver)

    await update.message.reply_text("Checking…")

    bol = driver.get("active_bol")
    last_status = driver.get("last_status")

    if last_status:
        text = f"{name} — {last_status}"
    elif bol:
        text = f"{name} — please assist"
        await post_or_edit_service_message(context, data, update.effective_chat.id, text)
        if bol["kind"] == "photo":
            await context.bot.send_photo(chat_id=SERVICE_GROUP_ID, photo=bol["ref"])
        elif bol["kind"] == "document":
            await context.bot.send_document(chat_id=SERVICE_GROUP_ID, document=bol["ref"])
        else:
            await context.bot.send_message(chat_id=SERVICE_GROUP_ID, text=bol["ref"])
        return
    else:
        text = f"{name} — please assist — NO BOL"

    await post_or_edit_service_message(context, data, update.effective_chat.id, text)


async def cmd_pod(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = storage.load()
    driver = storage.ensure_driver(data, update.effective_chat.id)
    driver["active_bol"] = None
    if driver.get("pending"):
        driver["pending"]["resolved"] = True
    storage.save(data)

    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("🟩 EMPTY", callback_data="status_empty"),
            InlineKeyboardButton("⬛ BOBTAIL", callback_data="status_bobtail"),
        ]]
    )
    await update.message.reply_text("POD received. What's your status?", reply_markup=keyboard)


async def on_status_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = storage.load()
    driver = storage.ensure_driver(data, query.message.chat.id)

    if query.data == "status_empty":
        AWAITING_TRAILER.add(query.message.chat.id)
        await query.edit_message_text("Send the trailer number (e.g. 12345).")
        return

    if query.data == "status_bobtail":
        driver["last_status"] = "BOBTAIL"
        storage.save(data)
        await query.edit_message_text("✅ Status saved: BOBTAIL")


async def on_plain_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in AWAITING_TRAILER:
        trailer = update.message.text.strip()
        data = storage.load()
        driver = storage.ensure_driver(data, chat_id)
        driver["last_status"] = f"EMPTY #{trailer}"
        storage.save(data)
        AWAITING_TRAILER.discard(chat_id)
        await update.message.reply_text(f"✅ Status saved: EMPTY #{trailer}")


async def cmd_pu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = storage.load()
    driver = storage.ensure_driver(data, update.effective_chat.id)
    await context.bot.send_message(chat_id=SERVICE_GROUP_ID, text=f"📦 {driver_label(driver)} — Picked up")
    await update.message.reply_text("✅ Pickup logged.")


async def cmd_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = storage.load()
    driver = storage.ensure_driver(data, update.effective_chat.id)
    await context.bot.send_message(chat_id=SERVICE_GROUP_ID, text=f"🚚 {driver_label(driver)} — Delivered")
    await update.message.reply_text("✅ Delivery logged.")


async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != SERVICE_GROUP_ID:
        return
    data = storage.load()
    target_gid = None

    if update.message.reply_to_message:
        mid = update.message.reply_to_message.message_id
        for gid, driver in data["drivers"].items():
            p = driver.get("pending")
            if p and p.get("service_msg_id") == mid:
                target_gid = gid
                break
    elif context.args:
        name = " ".join(context.args).lower()
        for gid, driver in data["drivers"].items():
            if (driver.get("name") or "").lower() == name:
                target_gid = gid
                break

    if not target_gid:
        await update.message.reply_text("Reply to the request with /done, or use /done DriverName.")
        return

    data["drivers"][target_gid]["pending"]["resolved"] = True
    storage.save(data)
    await update.message.reply_text("✅ Marked as done.")

    # let the driver know their request was resolved
    try:
        await context.bot.send_message(
            chat_id=int(target_gid),
            text="✅ Your request has been resolved by dispatch.",
        )
    except Exception as e:
        log.warning("Could not notify driver group %s: %s", target_gid, e)


async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != SERVICE_GROUP_ID:
        return
    data = storage.load()
    lines = []
    for driver in data["drivers"].values():
        p = driver.get("pending")
        if p and not p.get("resolved", False):
            lines.append(f"• {driver_label(driver)} (since {p['ts'][11:16]} UTC)")
    text = "⏳ Pending requests:\n" + "\n".join(lines) if lines else "✅ No pending requests."
    await update.message.reply_text(text)


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != SERVICE_GROUP_ID:
        return
    data = storage.load()
    records = [
        {"group_id": int(gid), "name": d.get("name")}
        for gid, d in data["drivers"].items() if d.get("name")
    ]
    await update.message.reply_text(json.dumps(records, ensure_ascii=False))


async def reminder_job(context: ContextTypes.DEFAULT_TYPE):
    data = storage.load()
    now = datetime.utcnow()
    changed = False
    for driver in data["drivers"].values():
        p = driver.get("pending")
        if not p or p.get("resolved", False):
            continue
        last = datetime.fromisoformat(p["last_reminder_ts"])
        if now - last >= timedelta(minutes=REMINDER_MINUTES):
            try:
                await context.bot.edit_message_text(
                    chat_id=SERVICE_GROUP_ID,
                    message_id=p["service_msg_id"],
                    text=f"{driver_label(driver)} — any news?",
                )
                p["last_reminder_ts"] = storage.timestamp()
                changed = True
            except Exception as e:
                log.warning("Reminder edit failed: %s", e)
    if changed:
        storage.save(data)


# ===========================================================================
# MAINTENANCE / PTI (Stage 2) — /admin /pti /ptidone /history /incident
# ===========================================================================
async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = storage.load()
    data["maintenance_group_id"] = update.effective_chat.id
    storage.save(data)
    await update.message.reply_text("✅ This group is now the maintenance/service group.")


def _pti_keyboard(data):
    excluded = data["pti_session"]["excluded_groups"]
    rows = []
    for gid, driver in data["drivers"].items():
        name = driver.get("name") or gid
        mark = "❌" if int(gid) in excluded else "✅"
        rows.append([InlineKeyboardButton(f"{mark} {name}", callback_data=f"toggle_{gid}")])
    rows.append([InlineKeyboardButton("🚀 Send PTI requests", callback_data="pti_send")])
    return InlineKeyboardMarkup(rows)


async def cmd_pti(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = storage.load()
    if not data["drivers"]:
        await update.message.reply_text("No driver groups registered yet. Use /register in each driver group first.")
        return

    data["pti_session"] = {
        "confirmed": False,
        "excluded_groups": [],
        "responses": {},
        "summary_msg_id": None,
        "started_ts": storage.timestamp(),
    }
    storage.save(data)
    await update.message.reply_text(
        "📋 Weekly PTI — tap a driver to exclude them, then press Send.",
        reply_markup=_pti_keyboard(data),
    )


async def on_pti_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = storage.load()
    session = data.get("pti_session")
    if not session:
        await query.answer("No active PTI round.")
        return

    if query.data.startswith("toggle_"):
        gid = int(query.data.split("_", 1)[1])
        excl = session["excluded_groups"]
        if gid in excl:
            excl.remove(gid)
        else:
            excl.append(gid)
        storage.save(data)
        await query.edit_message_reply_markup(reply_markup=_pti_keyboard(data))
        await query.answer()
        return

    if query.data == "pti_send":
        included = [(gid, d) for gid, d in data["drivers"].items() if int(gid) not in session["excluded_groups"]]
        for gid, driver in included:
            name = driver.get("name") or gid
            try:
                await context.bot.send_message(
                    chat_id=int(gid),
                    text=f"🔧 {name}, please complete your PTI today and send photos with /ptidone.",
                )
                session["responses"][gid] = False
            except Exception as e:
                log.warning("Could not message group %s: %s", gid, e)

        session["confirmed"] = True
        storage.save(data)
        await _post_or_update_summary(context, data)
        await query.edit_message_text(f"✅ PTI requests sent to {len(included)} drivers.")
        await query.answer()


async def _post_or_update_summary(context, data):
    session = data.get("pti_session")
    maint_gid = data.get("maintenance_group_id")
    if not session or not maint_gid:
        return
    total = len(session["responses"])
    done = sum(1 for v in session["responses"].values() if v)
    text = f"PTI progress: {progress_bar(done, total)}"

    if session.get("summary_msg_id"):
        try:
            await context.bot.delete_message(chat_id=maint_gid, message_id=session["summary_msg_id"])
        except Exception:
            pass

    msg = await context.bot.send_message(chat_id=maint_gid, text=text)
    session["summary_msg_id"] = msg.message_id
    storage.save(data)


async def cmd_ptidone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = storage.load()
    session = data.get("pti_session")
    gid = str(update.effective_chat.id)

    if not session or gid not in session.get("responses", {}):
        await update.message.reply_text("No active PTI request for this group right now.")
        return

    msg = update.message
    ref, kind = None, None
    if msg.photo:
        ref, kind = msg.photo[-1].file_id, "photo"
    elif msg.document:
        ref, kind = msg.document.file_id, "document"
    elif context.args:
        ref, kind = " ".join(context.args), "link"

    name = data["drivers"].get(gid, {}).get("name", gid)
    if ref:
        data["history"].setdefault(name, []).append(
            {"kind": kind, "ref": ref, "ts": storage.timestamp(), "type": "PTI"}
        )

    session["responses"][gid] = True
    storage.save(data)

    await update.message.reply_text("🙏 Thank you, safe trip.")
    await _post_or_update_summary(context, data)

    if all(session["responses"].values()):
        lines = []
        for g, done in session["responses"].items():
            dname = data["drivers"].get(g, {}).get("name", g)
            lines.append(f"{'🟢' if done else '🔴'} {dname}")
        maint_gid = data.get("maintenance_group_id")
        if maint_gid:
            await context.bot.send_message(chat_id=maint_gid, text="✅ PTI complete:\n" + "\n".join(lines))


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = storage.load()
    if update.effective_chat.id != data.get("maintenance_group_id"):
        return
    if not context.args:
        await update.message.reply_text("Usage: /history DriverName")
        return
    name = " ".join(context.args)
    records = data["history"].get(name, [])
    if not records:
        await update.message.reply_text(f"No history for {name}.")
        return
    lines = [f"{r['ts'][:16]} — {r['type']} ({r['kind']})" for r in records[-15:]]
    await update.message.reply_text(f"📁 {name} — last {len(lines)} records:\n" + "\n".join(lines))


async def cmd_incident(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = storage.load()
    gid = str(update.effective_chat.id)
    name = data["drivers"].get(gid, {}).get("name", "Unknown driver")
    details = update.message.text.partition(" ")[2].strip() or "No details provided."
    maint_gid = data.get("maintenance_group_id")
    if maint_gid:
        await context.bot.send_message(chat_id=maint_gid, text=f"🚨 INCIDENT — {name}\n{details}")
    await update.message.reply_text("🚨 Alert sent. Stay safe.")


async def daily_report_job(context: ContextTypes.DEFAULT_TYPE):
    data = storage.load()
    maint_gid = data.get("maintenance_group_id")
    if not maint_gid:
        return
    total_drivers = len(data["drivers"])
    session = data.get("pti_session")
    pti_line = "No active PTI round."
    if session:
        total = len(session["responses"])
        done = sum(1 for v in session["responses"].values() if v)
        pti_line = f"PTI: {progress_bar(done, total)}"
    await context.bot.send_message(
        chat_id=maint_gid,
        text=f"📊 Daily report\nDrivers registered: {total_drivers}\n{pti_line}",
    )


# ===========================================================================
# Main
# ===========================================================================
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # dispatch
    app.add_handler(CommandHandler("register", cmd_register))
    app.add_handler(CommandHandler(["BOL", "bol"], cmd_bol))
    app.add_handler(CommandHandler("t", cmd_t))
    app.add_handler(CommandHandler(["POD", "pod"], cmd_pod))
    app.add_handler(CommandHandler(["PU", "pu"], cmd_pu))
    app.add_handler(CommandHandler(["DEL", "del"], cmd_del))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("export", cmd_export))

    # maintenance / PTI
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("pti", cmd_pti))
    app.add_handler(CommandHandler("ptidone", cmd_ptidone))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("incident", cmd_incident))

    # shared
    app.add_handler(CallbackQueryHandler(on_status_button, pattern="^status_"))
    app.add_handler(CallbackQueryHandler(on_pti_button, pattern="^(toggle_|pti_send)"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_plain_text))
    app.add_handler(MessageHandler((filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND, cache_media))

    app.job_queue.run_repeating(reminder_job, interval=60, first=60)
    app.job_queue.run_daily(daily_report_job, time=dtime(hour=DAILY_REPORT_HOUR_UTC))

    log.info("Combined DEAC bot (Stage 3) starting…")
    start_keep_alive()
    app.run_polling()


if __name__ == "__main__":
    main()
