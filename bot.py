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
import re
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
AWAITING_COMPANY = set()  # group_ids currently waiting for a typed new company name
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
# DISPATCH (Stage 1) — /add /BOL /t /POD /PU /DEL /done /pending /export
# ===========================================================================
async def cmd_register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /add First Last")
        return
    name = " ".join(context.args)
    data = storage.load()
    driver = storage.ensure_driver(data, update.effective_chat.id, name=name)
    driver["user_id"] = update.effective_user.id
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
            if "not modified" in str(e).lower():
                # content is identical to what's already posted - nothing to do
                pending["last_reminder_ts"] = storage.timestamp()
                storage.save(data)
                return
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
        return

    if chat_id in AWAITING_COMPANY:
        name = update.message.text.strip()
        data = storage.load()
        companies = data.setdefault("companies", [])
        if name not in companies:
            companies.append(name)
        draft = data.get("update_draft", {})
        draft["company"] = name
        data["update_draft"] = draft
        storage.save(data)
        AWAITING_COMPANY.discard(chat_id)
        await update.message.reply_text(f"🏢 Company added and set: {name}")

        missing = missing_fields(draft)
        if missing:
            msg = await update.message.reply_text(
                "Still need:\n" + "\n".join(f"• {m}" for m in missing)
            )
            data["update_missing_msg_id"] = msg.message_id
            storage.save(data)
        else:
            await update.message.reply_text(build_template(draft))
            data["update_draft"] = {}
            storage.save(data)
        return

    data = storage.load()
    if chat_id == data.get("update_group_id"):
        await _handle_update_text(update, context, data, update.message.text)


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

    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"{d} kun" if d > 1 else "Har kuni", callback_data=f"interval_{d}")
          for d in (1, 2, 3, 4)],
         [InlineKeyboardButton(f"{d} kun", callback_data=f"interval_{d}") for d in (5, 6, 7)]]
    )
    await update.message.reply_text(
        "📊 Kunlik hisobot qancha kunda bir yuborilsin?", reply_markup=keyboard
    )


async def on_interval_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    days = int(query.data.split("_", 1)[1])
    data = storage.load()
    if query.message.chat.id != data.get("maintenance_group_id"):
        await query.answer("This only works in the maintenance group.")
        return
    data["report_interval_days"] = days
    storage.save(data)
    await query.answer()
    label = "har kuni" if days == 1 else f"{days} kunda bir marta"
    await query.edit_message_text(f"✅ Hisobot endi {label} yuboriladi. (/admin bilan qayta o'zgartirish mumkin)")


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
    if update.effective_chat.id != data.get("maintenance_group_id"):
        return  # silently ignore - /pti only works in the maintenance group

    if context.args:
        await _pti_lookup(update, context, data)
        return

    if not data["drivers"]:
        await update.message.reply_text("No driver groups registered yet. Use /add in each driver group first.")
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


async def _pti_lookup(update: Update, context: ContextTypes.DEFAULT_TYPE, data):
    """/pti DriverName [YYYY-MM-DD] — resend that driver's past PTI photos."""
    args = list(context.args)
    date_filter = None
    if re.match(r"^\d{4}-\d{2}-\d{2}$", args[-1]):
        date_filter = args.pop()
    name = " ".join(args).strip()
    if not name:
        await update.message.reply_text("Usage: /pti DriverName [YYYY-MM-DD]")
        return

    records = [r for r in data.get("history", {}).get(name, []) if r.get("type") == "PTI"]
    if date_filter:
        records = [r for r in records if r["ts"][:10] == date_filter]
    if not records:
        suffix = f" on {date_filter}" if date_filter else ""
        await update.message.reply_text(f"No PTI records found for {name}{suffix}.")
        return

    to_send = records if date_filter else [records[-1]]
    for r in to_send:
        caption = f"{r['ts'][:16]} — PTI — {name}"
        try:
            if r["kind"] == "photo":
                await context.bot.send_photo(chat_id=update.effective_chat.id, photo=r["ref"], caption=caption)
            elif r["kind"] == "document":
                await context.bot.send_document(chat_id=update.effective_chat.id, document=r["ref"], caption=caption)
            else:
                await update.message.reply_text(f"{caption}\n{r['ref']}")
        except Exception as e:
            log.warning("Could not resend PTI record for %s: %s", name, e)
            await update.message.reply_text(f"{caption} — (couldn't resend)")


async def on_pti_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = storage.load()
    if query.message.chat.id != data.get("maintenance_group_id"):
        await query.answer("This only works in the maintenance group.")
        return
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
            user_id = driver.get("user_id")
            try:
                if user_id:
                    mention = f'<a href="tg://user?id={user_id}">{name}</a>'
                    await context.bot.send_message(
                        chat_id=int(gid),
                        text=f"🔧 {mention}, please complete your PTI today and send photos with /ptidone.",
                        parse_mode="HTML",
                    )
                else:
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


def _build_pti_summary_text(data):
    session = data["pti_session"]
    total = len(session["responses"])
    done = sum(1 for v in session["responses"].values() if v)
    lines = [f"PTI progress: {done}/{total}"]
    for gid, is_done in session["responses"].items():
        dname = data["drivers"].get(gid, {}).get("name", gid)
        lines.append(f"{'✅' if is_done else '❌'} {dname}")
    return "\n".join(lines)


async def _post_or_update_summary(context, data):
    session = data.get("pti_session")
    maint_gid = data.get("maintenance_group_id")
    if not session or not maint_gid:
        return
    text = _build_pti_summary_text(data)

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

    # forward the actual photo/document/link to the maintenance group right away
    maint_gid = data.get("maintenance_group_id")
    if maint_gid and ref:
        caption = f"📸 PTI — {name} — {storage.timestamp()[:16]}"
        try:
            if kind == "photo":
                await context.bot.send_photo(chat_id=maint_gid, photo=ref, caption=caption)
            elif kind == "document":
                await context.bot.send_document(chat_id=maint_gid, document=ref, caption=caption)
            else:
                await context.bot.send_message(chat_id=maint_gid, text=f"{caption}\n{ref}")
        except Exception as e:
            log.warning("Could not forward PTI media to maintenance group: %s", e)

    await _post_or_update_summary(context, data)

    if all(session["responses"].values()):
        lines = []
        for g, done in session["responses"].items():
            dname = data["drivers"].get(g, {}).get("name", g)
            lines.append(f"{'🟢' if done else '🔴'} {dname}")
        maint_gid = data.get("maintenance_group_id")
        if maint_gid:
            await context.bot.send_message(chat_id=maint_gid, text="✅ PTI complete:\n" + "\n".join(lines))


async def cmd_ptiexcel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = storage.load()
    if update.effective_chat.id != data.get("maintenance_group_id"):
        return

    rows = []
    for name, records in data.get("history", {}).items():
        for r in records:
            if r.get("type") == "PTI":
                rows.append((r["ts"][:10], name, r["ts"][11:16], r["kind"]))
    if not rows:
        await update.message.reply_text("No PTI history yet.")
        return
    rows.sort(key=lambda row: row[0], reverse=True)

    import csv
    import io

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Date", "Driver", "Time (UTC)", "Type"])
    writer.writerows(rows)
    bio = io.BytesIO(buf.getvalue().encode("utf-8"))
    bio.name = "pti_history.csv"
    await context.bot.send_document(chat_id=update.effective_chat.id, document=bio, filename="pti_history.csv")


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

    recent = records[-15:]
    await update.message.reply_text(f"📁 {name} — last {len(recent)} records:")
    for r in recent:
        caption = f"{r['ts'][:16]} — {r['type']}"
        try:
            if r["kind"] == "photo":
                await context.bot.send_photo(chat_id=update.effective_chat.id, photo=r["ref"], caption=caption)
            elif r["kind"] == "document":
                await context.bot.send_document(chat_id=update.effective_chat.id, document=r["ref"], caption=caption)
            else:  # link
                await update.message.reply_text(f"{caption}\n{r['ref']}")
        except Exception as e:
            log.warning("Could not resend history item for %s: %s", name, e)
            await update.message.reply_text(f"{caption} — (couldn't resend file)")


async def cmd_incident(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = storage.load()
    gid = str(update.effective_chat.id)
    name = data["drivers"].get(gid, {}).get("name", "Unknown driver")
    details = update.message.text.partition(" ")[2].strip() or "No details provided."
    maint_gid = data.get("maintenance_group_id")
    if maint_gid:
        await context.bot.send_message(chat_id=maint_gid, text=f"🚨 INCIDENT — {name}\n{details}")
    await update.message.reply_text("🚨 Alert sent. Stay safe.")


async def cmd_sms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/sms First Last message text — used in the maintenance group.
    Sends the message to that driver's group, tagging their profile if
    known. Works with an attached photo/PDF too (as the caption)."""
    data = storage.load()
    if update.effective_chat.id != data.get("maintenance_group_id"):
        return

    msg = update.message
    raw = msg.text or msg.caption or ""
    args_text = raw.partition(" ")[2].strip()
    parts = args_text.split(" ", 2)
    if len(parts) < 2:
        await msg.reply_text("Usage: /sms First Last message text")
        return

    name = f"{parts[0]} {parts[1]}"
    text = parts[2] if len(parts) > 2 else ""

    target_gid, target_driver = None, None
    for gid, driver in data["drivers"].items():
        if (driver.get("name") or "").lower() == name.lower():
            target_gid, target_driver = gid, driver
            break

    if not target_gid:
        await msg.reply_text(f"Driver not found: {name}")
        return

    user_id = target_driver.get("user_id")
    dname = target_driver.get("name")
    if user_id:
        prefix = f'<a href="tg://user?id={user_id}">{dname}</a>\n'
        parse_mode = "HTML"
    else:
        prefix = f"{dname}\n"
        parse_mode = None

    try:
        if msg.photo:
            await context.bot.send_photo(
                chat_id=int(target_gid), photo=msg.photo[-1].file_id,
                caption=prefix + text, parse_mode=parse_mode,
            )
        elif msg.document:
            await context.bot.send_document(
                chat_id=int(target_gid), document=msg.document.file_id,
                caption=prefix + text, parse_mode=parse_mode,
            )
        else:
            await context.bot.send_message(
                chat_id=int(target_gid), text=prefix + text, parse_mode=parse_mode,
            )
        await msg.reply_text("✅ Sent.")
    except Exception as e:
        log.warning("Could not send /sms to %s: %s", name, e)
        await msg.reply_text("❌ Could not send message.")


async def daily_report_job(context: ContextTypes.DEFAULT_TYPE):
    data = storage.load()
    maint_gid = data.get("maintenance_group_id")
    if not maint_gid:
        return

    interval = data.get("report_interval_days", 1)
    today = datetime.utcnow().date().isoformat()
    last = data.get("last_report_date")
    if last:
        days_since = (datetime.fromisoformat(today) - datetime.fromisoformat(last)).days
        if days_since < interval:
            return  # not due yet

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
    data["last_report_date"] = today
    storage.save(data)


# ===========================================================================
# NEW-DRIVER UPDATE PARSER (/updadmin) — free-text driver info -> template
# ===========================================================================
REQUIRED_FIELDS = [
    ("name", "Driver Name"),
    ("company", "Hired Company name"),
    ("driver_type", "Driver Type"),
    ("phone", "Phone number"),
    ("email", "E-mail"),
    ("license", "License# (with state)"),
    ("unit", "Truck/Unit number"),
    ("year", "Truck Year"),
    ("make", "Truck Make"),
    ("made", "Truck Model (Made)"),
    ("vin", "VIN"),
    ("plate", "Plate (with state)"),
]
STATUS_OPTIONS = ["Pick up", "TERMINATED", "RETURNED", "TRUCK CHANGE", "SHOP", "ACCIDENT"]

# ordered longest-pattern-first so e.g. "Hired Company name" isn't cut short
# by a looser pattern matching first
FIELD_PATTERNS = {
    "company": r"(?:hired\s*)?company(?:\s*name)?",
    "driver_type": r"driver\s*type",
    "name": r"driver\s*name",
    "phone": r"ph[\-\s]*(?:nu)?#?|phone\s*#?|tel(?:ephone)?\s*#?",
    "email": r"e[\-\s]*mail",
    "license": r"license\s*#?|lic\s*#?|dl\s*#?",
    "unit": r"unit\s*#?|truck\s*#?|unit\s*number|truck\s*number",
    "year": r"year",
    "make": r"make",
    "made": r"made|model",
    "vin": r"vin\s*#?",
    "plate": r"plate\s*#?|tag\s*#?",
    "status": r"status",
}
# longest regex source first, so "hired company name" matches before "company"
_LABEL_ALT = "|".join(
    f"(?:{p})" for p in sorted(FIELD_PATTERNS.values(), key=len, reverse=True)
)
_LABEL_RE = re.compile(_LABEL_ALT, re.IGNORECASE)


def _split_line_multi(line):
    """Splits one line into (label_text, value) pairs, handling lines that
    contain more than one 'Label: value' pair (e.g. 'Year: 2023 Make: FRHT').
    A candidate label is only accepted if it's immediately followed by ':' or
    '#', or already has a '#' embedded in its own match (e.g. 'License#') —
    otherwise a value that happens to *contain* a label word (e.g. Driver
    Type: Company) would wrongly be read as a new field."""
    raw_matches = list(
        re.finditer(rf"(?P<label>{_LABEL_ALT})(?P<punct>\s*[:#])?", line, re.IGNORECASE)
    )
    matches = [m for m in raw_matches if m.group("punct") or "#" in m.group("label")]
    if not matches:
        return []
    segments = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(line)
        value = line[start:end].strip(" \t:#-")
        segments.append((m.group("label"), value))
    return segments


def parse_driver_fields(text):
    """Pulls recognizable fields out of free-form text:
    - labeled lines, including several labels on one line
    - unlabeled values it can still recognize by shape (email, VIN, phone)
    """
    found = {}
    for raw_line in text.splitlines():
        line = raw_line.strip().lstrip("💁📰👨🚛📍").strip()
        if not line:
            continue
        for label_text, val in _split_line_multi(line):
            if not val:
                continue
            for key, pat in FIELD_PATTERNS.items():
                if re.fullmatch(pat, label_text.strip(), re.IGNORECASE):
                    found[key] = val
                    break

    # context-based fallback for values sent without any label at all
    if "email" not in found:
        m = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)
        if m:
            found["email"] = m.group(0)
    if "vin" not in found:
        m = re.search(r"\b[A-HJ-NPR-Z0-9]{17}\b", text.upper())
        if m:
            found["vin"] = m.group(0)
    if "phone" not in found:
        m = re.search(r"(\+?\d[\d\-\s\(\)]{8,}\d)", text)
        if m:
            found["phone"] = m.group(0).strip()

    return found


def missing_fields(draft):
    return [label for key, label in REQUIRED_FIELDS if not draft.get(key)]


def build_template(draft):
    status = draft.get("status") or "Pick up"
    return (
        f"💁 Driver Name: {draft.get('name','')}\n"
        f"📰 Hired Company name: {draft.get('company','')}\n"
        f"👨 Driver Type: {draft.get('driver_type','')}\n"
        f"Ph-nu# {draft.get('phone','')}\n"
        f"E-mail: {draft.get('email','')}\n"
        f"License# {draft.get('license','')}\n"
        f"🚛 Truck info:\n"
        f"Unit#: {draft.get('unit','')}\n"
        f"Year: {draft.get('year','')}\n"
        f"Make: {draft.get('make','')}\n"
        f"Made: {draft.get('made','')}\n"
        f"VIN: {draft.get('vin','')}\n"
        f"Plate: {draft.get('plate','')}\n\n"
        f"📍Status: {status}"
    )


def _company_keyboard(data):
    companies = data.get("companies", [])
    rows = [[InlineKeyboardButton(c, callback_data=f"company_{i}")] for i, c in enumerate(companies)]
    rows.append([InlineKeyboardButton("➕ Add new company", callback_data="company_add_new")])
    return InlineKeyboardMarkup(rows)


def _confirm_keyboard():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Confirm & Post", callback_data="updconfirm"),
            InlineKeyboardButton("✏️ Edit a field", callback_data="updedit_hint"),
        ]]
    )


def _find_duplicates(data, draft, exclude_name=None):
    """Returns a list of warning strings if this VIN/license/plate is
    already used by a different driver profile."""
    warnings = []
    for key, label in (("vin", "VIN"), ("license", "License#"), ("plate", "Plate")):
        val = (draft.get(key) or "").strip().lower()
        if not val:
            continue
        for pname, profile in data.get("driver_profiles", {}).items():
            if exclude_name and pname == exclude_name:
                continue
            if (profile.get(key) or "").strip().lower() == val:
                warnings.append(f"⚠️ {label} '{draft.get(key)}' is already used by {profile.get('name', pname)}.")
    return warnings


async def cmd_updadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = storage.load()
    data["update_group_id"] = update.effective_chat.id
    data.setdefault("companies", [])
    data.setdefault("driver_profiles", {})
    data["update_draft"] = {}
    data["update_missing_msg_id"] = None
    storage.save(data)
    await update.message.reply_text("✅ This group is now the driver-update group.")


async def _present_next_step(update_or_query, context, data, chat_id):
    """Shared logic: given the current draft, either ask for company,
    ask for missing fields, or show the confirm-and-post preview."""
    draft = data.get("update_draft", {})

    async def send(text, **kwargs):
        if hasattr(update_or_query, "message") and update_or_query.message is None:
            return await context.bot.send_message(chat_id=chat_id, text=text, **kwargs)
        return await update_or_query.message.reply_text(text, **kwargs)

    if not draft.get("company"):
        msg = await send("🏢 Select the company, or add a new one:", reply_markup=_company_keyboard(data))
        data["update_missing_msg_id"] = msg.message_id
        storage.save(data)
        return

    missing = missing_fields(draft)
    if missing:
        msg = await send("Still need:\n" + "\n".join(f"• {m}" for m in missing))
        data["update_missing_msg_id"] = msg.message_id
        storage.save(data)
        return

    warnings = _find_duplicates(data, draft)
    preview = build_template(draft)
    if warnings:
        preview = "\n".join(warnings) + "\n\n" + preview
    msg = await send(preview, reply_markup=_confirm_keyboard())
    data["update_missing_msg_id"] = msg.message_id
    storage.save(data)


async def _handle_update_text(update: Update, context: ContextTypes.DEFAULT_TYPE, data, text):
    """Called for any plain text sent in the registered update group."""
    draft = data.get("update_draft", {})
    draft.update(parse_driver_fields(text))
    data["update_draft"] = draft
    storage.save(data)

    old_msg_id = data.get("update_missing_msg_id")
    if old_msg_id:
        try:
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=old_msg_id)
        except Exception:
            pass
        data["update_missing_msg_id"] = None
        storage.save(data)

    await _present_next_step(update, context, data, update.effective_chat.id)


async def on_company_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = storage.load()
    companies = data.get("companies", [])

    if query.data == "company_add_new":
        AWAITING_COMPANY.add(query.message.chat.id)
        await query.edit_message_text("Type the new company name:")
        await query.answer()
        return

    idx = int(query.data.split("_", 1)[1])
    if 0 <= idx < len(companies):
        draft = data.get("update_draft", {})
        draft["company"] = companies[idx]
        data["update_draft"] = draft
        data["update_missing_msg_id"] = None
        storage.save(data)
        await query.edit_message_text(f"🏢 Company set: {companies[idx]}")
        await _present_next_step(query, context, data, query.message.chat.id)
    await query.answer()


async def on_update_confirm_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = storage.load()
    draft = data.get("update_draft", {})

    if query.data == "updedit_hint":
        await query.answer("Just resend the field you want to fix, e.g. 'Phone: 555-1234'.", show_alert=True)
        return

    if query.data == "updconfirm":
        if not draft.get("name"):
            await query.answer("Missing driver name.")
            return
        key = draft["name"].strip().lower()
        profile = dict(draft)
        profile["ts"] = storage.timestamp()
        data.setdefault("driver_profiles", {})[key] = profile
        data["update_draft"] = {}
        data["update_missing_msg_id"] = None
        storage.save(data)
        await query.edit_message_text(build_template(profile) + "\n\n✅ Saved.")
        await query.answer()


async def cmd_finddriver(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = storage.load()
    if update.effective_chat.id != data.get("update_group_id"):
        return
    if not context.args:
        await update.message.reply_text("Usage: /finddriver <name, plate, VIN, or license>")
        return
    query = " ".join(context.args).strip().lower()
    matches = []
    for pname, profile in data.get("driver_profiles", {}).items():
        haystack = " ".join(str(profile.get(k, "")) for k in ("name", "plate", "vin", "license")).lower()
        if query in haystack:
            matches.append(profile)
    if not matches:
        await update.message.reply_text("No matching driver found.")
        return
    for profile in matches[:5]:
        await update.message.reply_text(build_template(profile))


async def cmd_updedit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = storage.load()
    if update.effective_chat.id != data.get("update_group_id"):
        return
    if not context.args:
        await update.message.reply_text("Usage: /updedit Driver Name")
        return
    name = " ".join(context.args).strip()
    key = name.lower()
    profile = data.get("driver_profiles", {}).get(key)
    if not profile:
        await update.message.reply_text(f"No saved profile for {name}.")
        return
    data["update_draft"] = dict(profile)
    data["update_missing_msg_id"] = None
    storage.save(data)
    await update.message.reply_text(
        f"✏️ Editing {name}. Send the field(s) you want to change (e.g. 'Phone: 555-1234'), "
        "then confirm when done."
    )
    await _present_next_step(update, context, data, update.effective_chat.id)


async def cmd_exportdrivers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = storage.load()
    if update.effective_chat.id != data.get("update_group_id"):
        return
    profiles = data.get("driver_profiles", {})
    if not profiles:
        await update.message.reply_text("No saved driver profiles yet.")
        return

    import csv
    import io

    buf = io.StringIO()
    fieldnames = [key for key, _ in REQUIRED_FIELDS] + ["status"]
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    for profile in profiles.values():
        writer.writerow({k: profile.get(k, "") for k in fieldnames})

    bio = io.BytesIO(buf.getvalue().encode("utf-8"))
    bio.name = "drivers.csv"
    await context.bot.send_document(chat_id=update.effective_chat.id, document=bio, filename="drivers.csv")


# ===========================================================================
# Main
# ===========================================================================
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # dispatch
    app.add_handler(CommandHandler("add", cmd_register))
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
    app.add_handler(CommandHandler("updadmin", cmd_updadmin))
    app.add_handler(CommandHandler("finddriver", cmd_finddriver))
    app.add_handler(CommandHandler("updedit", cmd_updedit))
    app.add_handler(CommandHandler("exportdrivers", cmd_exportdrivers))
    app.add_handler(CommandHandler("pti", cmd_pti))
    app.add_handler(CommandHandler("ptidone", cmd_ptidone))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("ptiexcel", cmd_ptiexcel))
    app.add_handler(CommandHandler("incident", cmd_incident))
    app.add_handler(CommandHandler("sms", cmd_sms))

    # shared
    app.add_handler(CallbackQueryHandler(on_status_button, pattern="^status_"))
    app.add_handler(CallbackQueryHandler(on_pti_button, pattern="^(toggle_|pti_send)"))
    app.add_handler(CallbackQueryHandler(on_interval_button, pattern="^interval_"))
    app.add_handler(CallbackQueryHandler(on_company_button, pattern="^company_"))
    app.add_handler(CallbackQueryHandler(on_update_confirm_button, pattern="^(updconfirm|updedit_hint)$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_plain_text))
    app.add_handler(MessageHandler((filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND, cache_media))

    app.job_queue.run_repeating(reminder_job, interval=60, first=60)
    app.job_queue.run_daily(daily_report_job, time=dtime(hour=DAILY_REPORT_HOUR_UTC))

    log.info("Combined DEAC bot (Stage 3) starting…")
    start_keep_alive()
    app.run_polling()


if __name__ == "__main__":
    main()
