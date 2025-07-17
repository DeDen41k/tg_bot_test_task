import logging
import traceback
from tempfile import NamedTemporaryFile
from string import Template as StringTemplate

from decouple import config
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)

from mindee import ClientV2, InferencePredictOptions
from mindee.parsing.v2 import InferenceResponse

# Load tokens
TG_TOKEN = config("TELEGRAM_TOKEN")
MINDEE_API_KEY = config("MINDEE_API_KEY")
VEHICLE_MODEL_ID = config("VEHICLE_MODEL_ID")
PASSPORT_MODEL_ID = config("PASSPORT_MODEL_ID")

# Initialize Mindee client
mindee_client = ClientV2(MINDEE_API_KEY)

# State definitions
ASK_PASSPORT, ASK_CAR_DOC, CONFIRM_DATA, PRICE_CONFIRM, PRICE_RECONFIRM, AFTER_POLICY = range(6)

# In-memory data
user_data_storage = {}

# Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)


# Starting message
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привіт! Я — бот для покупки автострахування.\n"
        "Надішліть, будь ласка, *фото вашого паспорта*.\n\n"
        "Якщо помилилися — введіть /cancel.",
        parse_mode="Markdown"
    )
    return ASK_PASSPORT


# Downloading photo
async def download_photo(file, context):
    tg_file = await context.bot.get_file(file.file_id)
    with NamedTemporaryFile(delete=False, suffix=".jpg") as f:
        await tg_file.download_to_drive(custom_path=f.name)
        return f.name


# Extracting passport data using Mindee API
def extract_passport_data(image_path):
    try:
        input_source = mindee_client.source_from_path(image_path)
        options = InferencePredictOptions(model_id=PASSPORT_MODEL_ID, rag=False)
        response: InferenceResponse = mindee_client.enqueue_and_parse(input_source, options)
        fields = response.inference.result.fields

        surnames_field = fields.get("surnames")
        given_names_field = fields.get("given_names")
        passport_number_field = fields.get("passport_number")

        surnames = surnames_field.value if surnames_field else ""
        given_names = given_names_field.value if given_names_field else ""

        full_name = (given_names + " " + surnames).strip() or "Unknown"
        passport_number = passport_number_field.value if passport_number_field else "Unknown"

        return {
            "full_name": full_name,
            "passport_number": passport_number
        }

    except Exception as e:
        print("Mindee passport extraction error:", e)
        return {
            "full_name": "Unknown",
            "passport_number": "Unknown"
        }


# Extracting vin data using Mindee API
def extract_vehicle_data(image_path):
    try:
        input_source = mindee_client.source_from_path(image_path)

        options = InferencePredictOptions(
            model_id=VEHICLE_MODEL_ID,
            rag=False,
        )

        response: InferenceResponse = mindee_client.enqueue_and_parse(input_source, options)

        fields = response.inference.result.fields
        print("VEHICLE fields:", fields)

        car_model_field = fields.get("car_model")
        car_model = car_model_field.value if car_model_field else "Unknown"

        car_brand_field = fields.get("car_brand")
        car_brand = car_brand_field.value if car_brand_field else "Unknown"

        vin_number_field = fields.get("vin_number")
        vin_number = vin_number_field.value if vin_number_field else "Unknown"

        return {
            "car_brand": car_brand,
            "car_model": car_model,
            "vin_number": vin_number
        }

    except Exception as e:
        print("Mindee vehicle extraction error:", e)
        return {
            "car_brand": "Unknown",
            "car_model": "Unknown",
            "vin_number": "Unknown"
        }


# Receiving passport data
async def receive_passport(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo = update.message.photo[-1]
    image_path = await download_photo(photo, context)

    extracted = extract_passport_data(image_path)

    user_data_storage[update.message.from_user.id] = {
        "passport_photo": image_path,
        "extracted": extracted
    }

    await update.message.reply_text("Дякую! Тепер, будь ласка, надішліть фото документа на авто.")
    return ASK_CAR_DOC


# Receiving vin data
async def receive_car_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo = update.message.photo[-1]
    image_path = await download_photo(photo, context)
    car_data = extract_vehicle_data(image_path)

    user_id = update.message.from_user.id
    user_data_storage[user_id]["car_doc"] = image_path
    user_data_storage[user_id]["extracted"].update(car_data)

    data = user_data_storage[user_id]["extracted"]

    confirmation_msg = (
        f"Ось що я знайшов:\n"
        f"👤 Ім'я: {data.get('full_name', 'Невідомо')}\n"
        f"📄 Паспорт: {data.get('passport_number', 'Невідомо')}\n"
        f"🚗 Авто: {data.get('car_brand', 'Невідомо')} {data.get('car_model', 'Невідомо')}\n"
        f"🔧 VIN: {data.get('vin_number', 'Невідомо')}\n\n"
        "Все правильно?"
    )
    reply_keyboard = [["Так", "Ні"]]
    await update.message.reply_text(
        confirmation_msg,
        reply_markup=ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=True, resize_keyboard=True),
    )
    return CONFIRM_DATA


# Data confirmation logic
async def confirm_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text.lower() == "так":
        reply_keyboard = [["Так", "Ні"]]
        await update.message.reply_text("Ціна страховки становить 100 USD. Приймаєте?",
                                        reply_markup=ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=True, resize_keyboard=True))
        return PRICE_CONFIRM
    else:
        await update.message.reply_text("Окей, надішліть, будь ласка, фото паспорта ще раз.")
        return ASK_PASSPORT


# Price confirmation logic
async def handle_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text.lower() == "так":
        return await issue_policy(update, context)
    else:
        reply_keyboard = [["Так", "Ні"]]
        await update.message.reply_text(
            "Вибачте, але на жаль, ціна фіксована (100 USD). Згодні?",
            reply_markup=ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=True, resize_keyboard=True),
        )
        return PRICE_RECONFIRM


# Continuation of confirmation logic
async def handle_reconfirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text.lower() == "так":
        return await issue_policy(update, context)
    else:
        await update.message.reply_text("Добре. Сподіваюся побачити вас наступного разу!")
        return ConversationHandler.END


# Sending the result
async def issue_policy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = user_data_storage[update.message.from_user.id]["extracted"]

    template_text = """
Страховий Поліс

Ім'я застрахованого: $full_name
Номер паспорта: $passport_number
Автомобіль: $car_brand $car_model
VIN: $vin_number

Сума до сплати: 100 USD

Цей документ є підтвердженням оформлення автострахування.
    """
    template = StringTemplate(template_text)
    policy_text = template.substitute(
        full_name=data.get("full_name", "Unknown"),
        passport_number=data.get("passport_number", "Unknown"),
        car_brand=data.get("car_brand", "Unknown"),
        car_model=data.get("car_model", "Unknown"),
        vin_number=data.get("vin_number", "Unknown"),
    )

    await update.message.reply_text("✅ Страховий поліс створено. Ось ваш документ:")
    await update.message.reply_text(policy_text.strip())

    reply_keyboard = [["Дякую", "Створити ще один поліс"]]
    await update.message.reply_text(
        "Бажаєте створити ще один страховий поліс?",
        reply_markup=ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=True, resize_keyboard=True),
    )
    return AFTER_POLICY


# Alternative ending
async def after_policy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.lower()
    if "дякую" in text:
        await update.message.reply_text("Дякую за використання бота! До зустрічі.")
        return ConversationHandler.END
    else:
        return await start(update, context)


# /cancel command logic
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Скасовано. Для початку введіть /start")
    return ConversationHandler.END


# Wrong user input
async def handle_unexpected_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Я очікую фото. Будь ласка, надішліть фото, як було запрошено.")


# In case of errors
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logging.error("Unhandled exception: %s", traceback.format_exc())
    if isinstance(update, Update) and update.message:
        await update.message.reply_text("Сталася помилка(( Спробуйте ще раз або введіть /start")


# Running the bot
if __name__ == "__main__":
    app = ApplicationBuilder().token(TG_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASK_PASSPORT: [
                MessageHandler(filters.PHOTO, receive_passport),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_unexpected_input),
            ],
            ASK_CAR_DOC: [
                MessageHandler(filters.PHOTO, receive_car_doc),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_unexpected_input),
            ],
            CONFIRM_DATA: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_data)],
            PRICE_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_price)],
            PRICE_RECONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_reconfirm)],
            AFTER_POLICY: [MessageHandler(filters.TEXT & ~filters.COMMAND, after_policy)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(conv_handler)
    app.add_error_handler(error_handler)
    app.run_polling()