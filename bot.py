import os
import json
import requests
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# --- Config ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
TEAMS_WEBHOOK = os.environ.get("TEAMS_WEBHOOK")

# --- Conversation states ---
TRAILER, COLLECTION, COUNTRY, PORT, DOCS, NOTES, CONFIRM = range(7)

COUNTRIES = [["UK", "Ireland", "France"], ["Germany", "Netherlands", "Belgium"], ["Spain", "Italy", "Poland"], ["Other"]]
PORTS = [["Dublin Port", "Rosslare"], ["Holyhead", "Dover"], ["Calais", "Cherbourg"], ["Other"]]

# --- Distance lookup ---
DISTANCES = {
    "dublin port":  {"km": 165, "hrs": 2.5},
    "rosslare":     {"km": 240, "hrs": 3.2},
    "holyhead":     {"km": 95,  "hrs": 1.5},
    "dover":        {"km": 120, "hrs": 1.8},
    "calais":       {"km": 130, "hrs": 1.9},
    "cherbourg":    {"km": 210, "hrs": 3.0},
}

def get_distance(port):
    key = port.lower().strip()
    for k, v in DISTANCES.items():
        if k in key:
            h = int(v["hrs"])
            m = int((v["hrs"] - h) * 60)
            prep = max(0, v["hrs"] - 0.5)
            ph = int(prep)
            pm = int((prep - ph) * 60)
            return f"~{v['km']} km | ~{h}h {m}m drive | ~{ph}h {pm}m to prep docs"
    return "Distance not calculated"

def post_to_teams(data):
    port_info = get_distance(data.get("port", ""))
    body = {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": [
                        {
                            "type": "TextBlock",
                            "text": "🚛 New Driver Submission",
                            "weight": "Bolder",
                            "size": "Medium",
                            "color": "Accent"
                        },
                        {
                            "type": "FactSet",
                            "facts": [
                                {"title": "Trailer", "value": data.get("trailer", "—")},
                                {"title": "Loaded", "value": f"{data.get('collection', '—')}, {data.get('country', '—')}"},
                                {"title": "Port", "value": data.get("port", "—")},
                                {"title": "Distance", "value": port_info},
                                {"title": "Documents", "value": data.get("docs", "None uploaded")},
                                {"title": "Notes", "value": data.get("notes", "None")},
                                {"title": "Driver chat ID", "value": str(data.get("chat_id", "—"))},
                            ]
                        }
                    ],
                    "actions": (
                        [
                            {
                                "type": "Action.OpenUrl",
                                "title": "📎 View uploaded document",
                                "url": data["doc_url"],
                            }
                        ]
                        if data.get("doc_url")
                        else []
                    ),
                }
            }
        ]
    }
    try:
        r = requests.post(TEAMS_WEBHOOK, json=body, timeout=10)
        return r.status_code == 202
    except Exception as e:
        print(f"Teams post error: {e}")
        return False

# --- Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "👋 Hi! I'm the *O'Toole Transport* customs bot.\n\nI'll collect your load details and notify the customs team straight away.\n\nLet's start — what is your *trailer number*?",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove()
    )
    return TRAILER

async def get_trailer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["trailer"] = update.message.text.strip().upper()
    await update.message.reply_text(
        f"Got it — *{context.user_data['trailer']}* ✅\n\nWhere did you *collect the load*? (Type the town or city)",
        parse_mode="Markdown"
    )
    return COLLECTION

async def get_collection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["collection"] = update.message.text.strip().title()
    await update.message.reply_text(
        "Which *country* did you load in?",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(COUNTRIES, one_time_keyboard=True, resize_keyboard=True)
    )
    return COUNTRY

async def get_country(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["country"] = update.message.text.strip()
    await update.message.reply_text(
        "Which *port* are you heading to?",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(PORTS, one_time_keyboard=True, resize_keyboard=True)
    )
    return PORT

async def get_port(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["port"] = update.message.text.strip()
    context.user_data["chat_id"] = update.effective_chat.id
    await update.message.reply_text(
        "Do you have any *MRN documents or photos* from the collection point?\n\nSend them now or tap Skip 👇",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([["Skip"]], one_time_keyboard=True, resize_keyboard=True)
    )
    return DOCS

async def get_docs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text and update.message.text.strip().lower() == "skip":
        context.user_data["docs"] = "None uploaded"
        context.user_data["doc_url"] = None
    elif update.message.photo:
        try:
            file = await update.message.photo[-1].get_file()
            context.user_data["doc_url"] = file.file_path
            context.user_data["docs"] = "📷 Photo uploaded"
        except Exception as e:
            print(f"File fetch error: {e}")
            context.user_data["doc_url"] = None
            context.user_data["docs"] = "📷 Photo uploaded (link unavailable)"
    elif update.message.document:
        try:
            file = await update.message.document.get_file()
            context.user_data["doc_url"] = file.file_path
            context.user_data["docs"] = f"📄 {update.message.document.file_name}"
        except Exception as e:
            print(f"File fetch error: {e}")
            context.user_data["doc_url"] = None
            context.user_data["docs"] = f"📄 {update.message.document.file_name} (link unavailable)"
    else:
        context.user_data["docs"] = update.message.text or "None uploaded"
        context.user_data["doc_url"] = None

    await update.message.reply_text(
        "Any *additional notes* for the customs team?\n\nType them now or tap Skip 👇",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([["Skip"]], one_time_keyboard=True, resize_keyboard=True)
    )
    return NOTES

async def get_notes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    context.user_data["notes"] = "None" if text.lower() == "skip" else text

    d = context.user_data
    port_info = get_distance(d.get("port", ""))
    summary = (
        f"*Here's your summary — does everything look correct?*\n\n"
        f"🚛 *Trailer:* {d.get('trailer')}\n"
        f"📍 *Loaded:* {d.get('collection')}, {d.get('country')}\n"
        f"⚓ *Port:* {d.get('port')}\n"
        f"🛣 *Distance:* {port_info}\n"
        f"📄 *Docs:* {d.get('docs')}\n"
        f"📝 *Notes:* {d.get('notes')}"
    )
    await update.message.reply_text(
        summary,
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([["✅ Send to customs", "❌ Start again"]], one_time_keyboard=True, resize_keyboard=True)
    )
    return CONFIRM

async def confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "start again" in update.message.text.lower() or "❌" in update.message.text:
        await update.message.reply_text("No problem — let's start over.", reply_markup=ReplyKeyboardRemove())
        return await start(update, context)

    success = post_to_teams(context.user_data)
    if success:
        await update.message.reply_text(
            "✅ *Customs team notified!*\n\nYour submission has been sent to the O'Toole customs team. They'll message you here if they need anything.\n\n*Safe drive!* 🚛",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardRemove()
        )
    else:
        await update.message.reply_text(
            "⚠️ Something went wrong sending to Teams. Please call the customs team directly.",
            reply_markup=ReplyKeyboardRemove()
        )
    context.user_data.clear()
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled. Type /start to begin again.", reply_markup=ReplyKeyboardRemove())
    context.user_data.clear()
    return ConversationHandler.END

# --- Main ---
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start), MessageHandler(filters.TEXT & ~filters.COMMAND, start)],
        states={
            TRAILER:    [MessageHandler(filters.TEXT & ~filters.COMMAND, get_trailer)],
            COLLECTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_collection)],
            COUNTRY:    [MessageHandler(filters.TEXT & ~filters.COMMAND, get_country)],
            PORT:       [MessageHandler(filters.TEXT & ~filters.COMMAND, get_port)],
            DOCS:       [MessageHandler(filters.TEXT | filters.PHOTO | filters.Document.ALL, get_docs)],
            NOTES:      [MessageHandler(filters.TEXT & ~filters.COMMAND, get_notes)],
            CONFIRM:    [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(conv)
    print("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
