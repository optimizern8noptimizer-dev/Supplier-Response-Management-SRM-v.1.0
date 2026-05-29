"""
Telegram Bot — Supplier Response Management
Позволяет загружать ответы поставщиков и получать уведомления.
"""
import asyncio
import logging
import os
import tempfile

import httpx
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN      = os.getenv("TELEGRAM_BOT_TOKEN", "")
BACKEND_URL    = os.getenv("BACKEND_URL", "http://backend:8000")
ALLOWED_USERS  = [int(x) for x in os.getenv("TELEGRAM_ALLOWED_USERS", "").split(",") if x.strip()]

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher(storage=MemoryStorage())


class UploadStates(StatesGroup):
    waiting_file         = State()
    waiting_claim_number = State()


# ─────────────────────────────────────────────────────────────────────────────
# Middleware: проверка доступа
# ─────────────────────────────────────────────────────────────────────────────

def is_allowed(user_id: int) -> bool:
    return not ALLOWED_USERS or user_id in ALLOWED_USERS


# ─────────────────────────────────────────────────────────────────────────────
# Команды
# ─────────────────────────────────────────────────────────────────────────────

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if not is_allowed(message.from_user.id):
        return

    await message.answer(
        "👋 *Система анализа ответов поставщиков*\n\n"
        "Доступные команды:\n"
        "📤 /upload — загрузить ответ поставщика (PDF/фото)\n"
        "🔍 /status `<номер_претензии>` — проверить статус\n"
        "📊 /stats — сводная статистика\n"
        "❓ /help — помощь",
        parse_mode="Markdown"
    )


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    if not is_allowed(message.from_user.id):
        return
    await message.answer(
        "*Инструкция по работе с ботом*\n\n"
        "1️⃣ Отправьте /upload\n"
        "2️⃣ Прикрепите файл ответа поставщика (PDF или фото скана)\n"
        "3️⃣ Укажите номер претензии (если бот не определил автоматически)\n"
        "4️⃣ Дождитесь результата анализа (обычно 1-3 минуты на CPU)\n\n"
        "⚡ Анализ выполняет ИИ-модель локально, данные не покидают контур компании.",
        parse_mode="Markdown"
    )


@dp.message(Command("upload"))
async def cmd_upload(message: types.Message, state: FSMContext):
    if not is_allowed(message.from_user.id):
        return
    await message.answer(
        "📎 Отправьте файл ответа поставщика (PDF или фото).\n"
        "Поддерживаемые форматы: PDF, JPG, PNG"
    )
    await state.set_state(UploadStates.waiting_file)


@dp.message(Command("status"))
async def cmd_status(message: types.Message):
    if not is_allowed(message.from_user.id):
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Укажите номер претензии: /status 103827")
        return

    claim_number = parts[1].strip()
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(f"{BACKEND_URL}/api/claims/{claim_number}", timeout=10.0)
            if resp.status_code == 404:
                await message.answer(f"❌ Претензия {claim_number} не найдена в системе.")
                return
            data = resp.json()
        except Exception as e:
            await message.answer(f"❌ Ошибка подключения к серверу: {e}")
            return

    responses = data.get("responses", [])
    if not responses:
        status_text = "📭 Ответ поставщика не получен"
    else:
        last = responses[0]
        from services.llm import CLASSIFICATION_LABELS, RECOMMENDATION_LABELS, RISK_LABELS
        status_text = (
            f"Статус: {CLASSIFICATION_LABELS.get(last.get('classification'), last.get('classification', 'N/A'))}\n"
            f"Риск: {RISK_LABELS.get(last.get('risk_level'), last.get('risk_level', 'N/A'))}\n"
            f"Рекомендация: {RECOMMENDATION_LABELS.get(last.get('recommendation'), last.get('recommendation', 'N/A'))}\n"
            f"Обработка: {last.get('processing_status', 'N/A')}"
        )

    await message.answer(
        f"📋 *Претензия №{claim_number}*\n"
        f"Поставщик: {data.get('supplier', {}).get('name', 'N/A') if data.get('supplier') else 'N/A'}\n"
        f"Сумма: {data.get('total_penalty_amount', 0)} {data.get('currency', 'BYN')}\n\n"
        f"{status_text}",
        parse_mode="Markdown"
    )


@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    if not is_allowed(message.from_user.id):
        return

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(f"{BACKEND_URL}/api/stats", timeout=10.0)
            data = resp.json()
        except Exception as e:
            await message.answer(f"❌ Ошибка: {e}")
            return

    await message.answer(
        f"📊 *Статистика системы*\n\n"
        f"📋 Всего претензий: {data.get('total_claims', 0)}\n"
        f"⏳ Ожидают ответа: {data.get('pending', 0)}\n"
        f"✅ Обработано: {data.get('processed', 0)}\n"
        f"⚖️ Готово для юриста: {data.get('ready_for_legal', 0)}\n"
        f"💰 Общая сумма штрафов: {data.get('total_penalty_byn', 0):,.2f} BYN",
        parse_mode="Markdown"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Обработка файлов
# ─────────────────────────────────────────────────────────────────────────────

@dp.message(UploadStates.waiting_file, F.document | F.photo)
async def handle_document(message: types.Message, state: FSMContext):
    """Принимает файл или фото ответа поставщика."""
    wait_msg = await message.answer("⏳ Загружаю файл...")

    # Определяем файл
    if message.document:
        file_id   = message.document.file_id
        file_name = message.document.file_name or "response.pdf"
    elif message.photo:
        file_id   = message.photo[-1].file_id  # Берём максимальное разрешение
        file_name = "response.jpg"
    else:
        await wait_msg.edit_text("❌ Отправьте файл PDF или фото.")
        return

    # Скачиваем файл
    file = await bot.get_file(file_id)
    file_bytes = await bot.download_file(file.file_path)

    await state.update_data(
        file_bytes=file_bytes.read(),
        file_name=file_name,
    )
    await wait_msg.delete()

    await message.answer(
        "✅ Файл получен.\n\n"
        "Введите *номер претензии* (только цифры, например: `103827`)\n"
        "или напишите *авто* для автоматического определения из документа:",
        parse_mode="Markdown"
    )
    await state.set_state(UploadStates.waiting_claim_number)


@dp.message(UploadStates.waiting_claim_number)
async def handle_claim_number(message: types.Message, state: FSMContext):
    """Принимает номер претензии и отправляет на сервер."""
    user_input = message.text.strip()
    claim_number = None if user_input.lower() == "авто" else user_input

    data = await state.get_data()
    await state.clear()

    wait_msg = await message.answer("⏳ Отправляю на анализ...")

    # Отправляем в backend
    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            form_data = {
                "submitted_by": f"telegram:{message.from_user.username or message.from_user.id}",
                "response_type": "manual_upload",
            }
            if claim_number:
                form_data["claim_number"] = claim_number

            resp = await client.post(
                f"{BACKEND_URL}/api/responses/upload",
                data=form_data,
                files={"file": (data["file_name"], data["file_bytes"])},
            )

            if resp.status_code == 200:
                result = resp.json()
                await wait_msg.edit_text(
                    f"✅ *Файл принят на обработку*\n\n"
                    f"🔢 Претензия: `{result.get('claim_number')}`\n"
                    f"🆔 ID ответа: `{result.get('response_id')}`\n"
                    f"⏱️ Анализ запущен. На CPU займёт 1-5 минут.\n\n"
                    f"Результат придёт автоматически в чат.",
                    parse_mode="Markdown"
                )
            elif resp.status_code == 422:
                await wait_msg.edit_text(
                    "❓ Номер претензии не определён автоматически.\n"
                    "Попробуйте ещё раз командой /upload и укажите номер вручную."
                )
            else:
                error = resp.json().get("detail", "Неизвестная ошибка")
                await wait_msg.edit_text(f"❌ Ошибка сервера: {error}")

        except Exception as e:
            await wait_msg.edit_text(f"❌ Ошибка подключения: {e}")


# Обработчик случайных сообщений в состоянии ожидания файла
@dp.message(UploadStates.waiting_file)
async def handle_text_in_upload(message: types.Message):
    await message.answer("📎 Пожалуйста, отправьте файл (PDF или фото)")


# Общий обработчик неизвестных сообщений
@dp.message()
async def handle_unknown(message: types.Message):
    if not is_allowed(message.from_user.id):
        return
    await message.answer("Используйте /help для списка команд.")


# ─────────────────────────────────────────────────────────────────────────────
# Запуск
# ─────────────────────────────────────────────────────────────────────────────

async def main():
    logger.info("Telegram bot starting...")
    await dp.start_polling(bot, skip_updates=True)


if __name__ == "__main__":
    asyncio.run(main())
