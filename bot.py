import asyncio
import logging
import os
from datetime import datetime, timedelta

import aiohttp.web
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from auditor import Auditor
from config import BOT_TOKEN, OWNER_BIO, OWNER_TELEGRAM_ID, OWNER_TELEGRAM_USERNAME
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


def _quick_results(result: dict, prev: dict | None = None) -> str:
    scores = result["scores"]
    avg = result["average_score"]
    grade = result.get("letter_grade", "")
    top3 = set(result.get("top3_priority", []))

    lines = []
    for s in scores:
        marker = "(!)" if s["id"] in top3 else "   "
        lines.append(f"{marker} {_score_line(s['score'], s['name'])}")

    summary = f"Оценка: {avg}/10  [{grade}]"
    if prev:
        diff = round(avg - prev["average_score"], 1)
        sign = "+" if diff >= 0 else ""
        summary += f"  (было {prev['average_score']}/10, {sign}{diff})"

    return "\n".join(lines) + f"\n\n{summary}"


# ─── handlers ───────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if db.pool is not None:
        try:
            await db.ensure_user(update.effective_user)
        except Exception as exc:
            logger.error("ensure_user in cmd_start failed: %s", exc)
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
        "10. Скорость отклика сервера\n"
        "11. Видимость в ИИ-поисковиках (ChatGPT, Perplexity)\n\n"
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

    db_ok = db.pool is not None

    if db_ok:
        try:
            await db.ensure_user(user)
        except Exception as exc:
            logger.error("ensure_user failed: %s", exc)
            db_ok = False

    url = auditor.normalize_url(raw)
    if not url:
        await update.message.reply_text(
            "Это не похоже на ссылку.\n\n"
            "Пожалуйста, отправьте URL вашего сайта.\n"
            "Пример: https://example.com"
        )
        return

    if db_ok:
        try:
            if await db.has_active_audit(user.id):
                await update.message.reply_text(
                    "У вас уже идёт проверка. Дождитесь её завершения."
                )
                return
        except Exception as exc:
            logger.error("has_active_audit failed: %s", exc)
            db_ok = False

    prev = None
    if db_ok:
        try:
            prev = await db.get_last_audit(user.id, url)
            if prev:
                age = datetime.now() - prev["created_at"].replace(tzinfo=None)
                if age < timedelta(days=7):
                    days_left = 7 - age.days
                    await update.message.reply_text(
                        f"Этот сайт уже проверялся {prev['date']}.\n"
                        f"Повторная проверка откроется через {days_left} дн.\n\n"
                        "Хотите проверить другой сайт — пришлите другую ссылку."
                    )
                    return
        except Exception as exc:
            logger.error("get_last_audit failed: %s", exc)

    progress = await update.message.reply_text("Начинаю проверку сайта...")
    audit_id = None
    if db_ok:
        try:
            audit_id = await db.create_audit(user.id, url)
        except Exception as exc:
            logger.error("create_audit failed: %s", exc)
            db_ok = False

    try:
        result = await auditor.run_audit(url, progress)

        if result is None:
            if audit_id:
                try:
                    await db.fail_audit(audit_id)
                except Exception:
                    pass
            await progress.edit_text(
                "Сайт недоступен или не отвечает.\n\n"
                "Проверьте правильность ссылки и попробуйте ещё раз."
            )
            return

        if audit_id:
            try:
                await db.complete_audit(audit_id, result)
            except Exception as exc:
                logger.error("complete_audit failed: %s", exc)

        client_path = reporter.generate_client_report(result)
        owner_path = reporter.generate_owner_report(result, user)

        quick = _quick_results(result, prev)
        express = result.get("express_summary", "")

        summary_text = "Проверка завершена!"
        if express:
            summary_text += f"\n\n{express}"
        summary_text += f"\n\n{quick}\n\n(!) — приоритет исправления\n\nОтправляю полный отчёт..."

        await progress.edit_text(summary_text)

        with open(client_path, "rb") as f:
            domain = url.replace("https://", "").replace("http://", "").split("/")[0]
            await update.message.reply_document(
                document=f,
                filename=f"audit_{domain}.html",
                caption="Ваш полный отчёт по аудиту сайта.",
            )

        grade = result.get("letter_grade", "")
        avg = result["average_score"]

        keyboard = []
        if OWNER_TELEGRAM_USERNAME:
            keyboard.append([
                InlineKeyboardButton(
                    "Исправить проблемы — написать",
                    url=f"https://t.me/{OWNER_TELEGRAM_USERNAME}",
                )
            ])

        reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
        bio_block = f"\n\n{OWNER_BIO}" if OWNER_BIO else ""
        await update.message.reply_text(
            f"Сайт набрал {avg}/10 [{grade}].{bio_block}\n\n"
            "Хотите довести до оценки A (9-10)? "
            "Помогу исправить все выявленные проблемы.",
            reply_markup=reply_markup,
        )

        await _notify_owner(context, user, url, result, owner_path, prev)

    except Exception as exc:
        logger.error("Audit failed: %s", exc, exc_info=True)
        if audit_id:
            try:
                await db.fail_audit(audit_id)
            except Exception:
                pass
        await progress.edit_text(
            "Произошла ошибка при проверке. Попробуйте позже."
        )


async def _notify_owner(
    context, user, url: str, result: dict, owner_path: str, prev: dict | None = None
):
    try:
        name = user.full_name or user.first_name or "Не указано"
        handle = f"@{user.username}" if user.username else f"ID: {user.id}"
        avg = result["average_score"]
        grade = result.get("letter_grade", "")
        quick = _quick_results(result, prev)

        lead_label = "ГОРЯЧИЙ ЛИД (оценка < 5)!\n\n" if avg < 5 else ""
        header = (
            f"{lead_label}Новая проверка сайта!\n\n"
            f"Клиент: {name} ({handle})\n"
            f"Сайт: {url}\n"
            f"Оценка: {avg}/10 [{grade}]\n\n"
        )
        if result.get("express_summary"):
            header += f"{result['express_summary']}\n\n"
        header += quick

        await context.bot.send_message(OWNER_TELEGRAM_ID, header)
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


# ─── web API ─────────────────────────────────────────────────────────────────

@aiohttp.web.middleware
async def cors_middleware(request: aiohttp.web.Request, handler):
    if request.method == "OPTIONS":
        return aiohttp.web.Response(
            status=200,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type",
            },
        )
    response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


async def health_check(request: aiohttp.web.Request) -> aiohttp.web.Response:
    return aiohttp.web.json_response({"status": "ok"})


async def web_audit(request: aiohttp.web.Request) -> aiohttp.web.Response:
    try:
        data = await request.json()
    except Exception:
        return aiohttp.web.json_response({"error": "Неверный формат запроса"}, status=400)

    raw_url = (data.get("url") or "").strip()
    url = auditor.normalize_url(raw_url)
    if not url:
        return aiohttp.web.json_response({"error": "Неверный URL"}, status=400)

    try:
        result = await auditor.run_audit(url)
    except Exception as exc:
        logger.error("Web audit failed: %s", exc, exc_info=True)
        return aiohttp.web.json_response({"error": "Ошибка при проверке"}, status=500)

    if result is None:
        return aiohttp.web.json_response({"error": "Сайт недоступен или не отвечает"}, status=503)

    return aiohttp.web.json_response(result)


# ─── main ────────────────────────────────────────────────────────────────────

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled exception in handler: %s", context.error, exc_info=context.error)
    if isinstance(update, Update) and update.message:
        try:
            await update.message.reply_text("Произошла ошибка. Попробуйте позже.")
        except Exception:
            pass


async def post_init(app: Application):
    try:
        await db.connect()
        logger.info("Database connected")
    except Exception as exc:
        logger.error("Database connection failed: %s", exc, exc_info=True)


async def main():
    # Web server
    web_app = aiohttp.web.Application(middlewares=[cors_middleware])
    web_app.router.add_get("/health", health_check)
    web_app.router.add_post("/audit", web_audit)
    runner = aiohttp.web.AppRunner(web_app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    await aiohttp.web.TCPSite(runner, "0.0.0.0", port).start()
    logger.info("Web server started on :%d", port)

    # Telegram bot
    bot_app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )
    bot_app.add_handler(CommandHandler("start", cmd_start))
    bot_app.add_handler(CommandHandler("help", cmd_help))
    bot_app.add_handler(CommandHandler("delete", cmd_delete))
    bot_app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )
    bot_app.add_error_handler(error_handler)

    logger.info("Bot starting...")
    async with bot_app:
        await bot_app.start()
        await bot_app.updater.start_polling(drop_pending_updates=True)
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
