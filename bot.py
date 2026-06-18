import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from auditor import Auditor
from config import BOT_TOKEN, OWNER_TELEGRAM_ID, OWNER_TELEGRAM_USERNAME
from database import Database
from reporter import Reporter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

db = Database()
auditor = Auditor()
reporter = Reporter()


# ─── helpers ────────────────────────────────────────────────────────────────

def _score_line(score: int, name: str) -> str:
    if score >= 8:
        indicator = "+"
    elif score >= 5:
        indicator = "~"
    else:
        indicator = "!"
    return f"[{indicator}] {name}: {score}/10"


def _quick_results(scores: list, average: float) -> str:
    lines = [_score_line(s["score"], s["name"]) for s in scores]
    return "\n".join(lines) + f"\n\nСредняя оценка: {average}/10"


# ─── handlers ───────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await db.ensure_user(update.effective_user)
    await update.message.reply_text(
        "Привет! Я проверю ваш сайт по 10 критериям и пришлю детальный отчёт.\n\n"
        "Что проверяю:\n"
        "1. Скорость загрузки\n"
        "2. Мобильная версия\n"
        "3. SEO оптимизация\n"
        "4. Безопасность (SSL)\n"
        "5. Удобство навигации\n"
        "6. Качество контента\n"
        "7. Наличие CTA-кнопок\n"
        "8. Работоспособность форм\n"
        "9. Адаптивность дизайна\n"
        "10. Скорость отклика сервера\n\n"
        "Отправьте ссылку на сайт — и я начну проверку."
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Отправьте ссылку на сайт (например: https://example.com) "
        "и я проведу полный аудит.\n\n"
        "/start — начало работы\n"
        "/delete — удалить все ваши данные\n"
        "/help — эта справка"
    )


async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await db.delete_user_data(update.effective_user.id)
    await update.message.reply_text(
        "Все ваши данные (история проверок, профиль) удалены."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    raw = update.message.text.strip()

    await db.ensure_user(user)

    url = auditor.normalize_url(raw)
    if not url:
        await update.message.reply_text(
            "Это не похоже на ссылку.\n\n"
            "Пожалуйста, отправьте URL вашего сайта.\n"
            "Пример: https://example.com"
        )
        return

    if await db.has_active_audit(user.id):
        await update.message.reply_text(
            "У вас уже идёт проверка. Дождитесь её завершения."
        )
        return

    progress = await update.message.reply_text("Начинаю проверку сайта...")
    audit_id = await db.create_audit(user.id, url)

    try:
        result = await auditor.run_audit(url, progress)

        if result is None:
            await db.fail_audit(audit_id)
            await progress.edit_text(
                "Сайт недоступен или не отвечает.\n\n"
                "Проверьте правильность ссылки и попробуйте ещё раз."
            )
            return

        await db.complete_audit(audit_id, result)

        client_path = reporter.generate_client_report(result)
        owner_path = reporter.generate_owner_report(result, user)

        quick = _quick_results(result["scores"], result["average_score"])
        await progress.edit_text(
            f"Проверка завершена!\n\n{quick}\n\nОтправляю полный отчёт..."
        )

        with open(client_path, "rb") as f:
            domain = url.replace("https://", "").replace("http://", "").split("/")[0]
            await update.message.reply_document(
                document=f,
                filename=f"audit_{domain}.html",
                caption="Ваш полный отчёт по аудиту сайта.",
            )

        keyboard = []
        if OWNER_TELEGRAM_USERNAME:
            keyboard.append([
                InlineKeyboardButton(
                    "Исправить проблемы — написать",
                    url=f"https://t.me/{OWNER_TELEGRAM_USERNAME}",
                )
            ])

        reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
        await update.message.reply_text(
            f"Сайт набрал {result['average_score']}/10.\n\n"
            "Хотите довести сайт до 10/10? "
            "Я помогу исправить все выявленные проблемы.",
            reply_markup=reply_markup,
        )

        await _notify_owner(context, user, url, result, owner_path)

    except Exception as exc:
        logger.error("Audit failed: %s", exc, exc_info=True)
        await db.fail_audit(audit_id)
        await progress.edit_text(
            "Произошла ошибка при проверке. Попробуйте позже."
        )


async def _notify_owner(context, user, url: str, result: dict, owner_path: str):
    try:
        name = user.full_name or user.first_name or "Не указано"
        handle = f"@{user.username}" if user.username else f"ID: {user.id}"
        avg = result["average_score"]
        quick = _quick_results(result["scores"], avg)

        await context.bot.send_message(
            OWNER_TELEGRAM_ID,
            f"Новая проверка сайта!\n\n"
            f"Клиент: {name} ({handle})\n"
            f"Сайт: {url}\n"
            f"Средняя оценка: {avg}/10\n\n"
            f"{quick}",
        )
        with open(owner_path, "rb") as f:
            domain = url.replace("https://", "").replace("http://", "").split("/")[0]
            await context.bot.send_document(
                OWNER_TELEGRAM_ID,
                document=f,
                filename=f"owner_audit_{domain}.html",
                caption="Полный отчёт с рекомендациями.",
            )
    except Exception as exc:
        logger.error("Owner notify failed: %s", exc)


# ─── main ────────────────────────────────────────────────────────────────────

async def post_init(app: Application):
    await db.connect()
    logger.info("Database connected")


def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("delete", cmd_delete))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )

    logger.info("Bot starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
