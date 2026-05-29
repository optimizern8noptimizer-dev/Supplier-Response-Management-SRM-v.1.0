"""
Email Celery Tasks
Периодический опрос Exchange inbox и постановка писем на LLM-анализ.

Регистрируется в Celery beat через workers/tasks.py.
"""
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def register_email_tasks(celery_app, config_settings):
    """
    Динамически регистрирует задачу poll_exchange в Celery beat.
    Вызывается из workers/tasks.py при инициализации если EMAIL_ENABLED=true.
    """
    if not config_settings.EMAIL_ENABLED:
        logger.info("[Email] EMAIL_ENABLED=false, опрос Exchange отключён")
        return

    @celery_app.task(name="workers.email_tasks.poll_and_process_emails")
    def poll_and_process_emails():
        """
        Периодическая задача: опрашивает Exchange, создаёт SupplierResponse
        и ставит в очередь на LLM-анализ.
        """
        from database import get_db_session
        from models import SupplierResponse, Claim, Supplier
        from services.email_exchange import poll_exchange_inbox
        from services.ocr import extract_text, extract_claim_number_from_text
        from services.erp import erp_client
        from workers.tasks import process_supplier_response, run_async
        from main import _create_claim_from_erp

        results = poll_exchange_inbox()
        if not results:
            return {"processed": 0}

        db = get_db_session()
        queued = 0

        try:
            for email_data in results:
                try:
                    queued += _process_single_email(
                        email_data, db, erp_client,
                        process_supplier_response, run_async
                    )
                except Exception as e:
                    logger.error(f"[Email] Ошибка обработки письма: {e}", exc_info=True)

            db.commit()

        except Exception as e:
            db.rollback()
            logger.error(f"[Email] Ошибка транзакции: {e}", exc_info=True)
        finally:
            db.close()

        logger.info(f"[Email] Поставлено в очередь: {queued} писем")
        return {"processed": queued}

    # Регистрируем в beat schedule
    interval = config_settings.EMAIL_CHECK_INTERVAL_SECONDS
    celery_app.conf.beat_schedule["poll-exchange-inbox"] = {
        "task":     "workers.email_tasks.poll_and_process_emails",
        "schedule": interval,
    }
    logger.info(f"[Email] Задача poll_exchange зарегистрирована, интервал: {interval}s")


def _process_single_email(email_data: dict, db, erp_client, process_task, run_async) -> int:
    """
    Обрабатывает одно письмо из Exchange.
    Возвращает 1 если поставлено в очередь, 0 если пропущено.
    """
    from models import SupplierResponse, Claim, Supplier
    from services.ocr import extract_text, extract_claim_number_from_text

    message_id    = email_data["message_id"]
    claim_number  = email_data.get("claim_number")
    body_text     = email_data.get("body_text", "")
    attachments   = email_data.get("attachments", [])
    response_type = email_data["response_type"]
    submitted_by  = email_data["submitted_by"]
    received_at   = email_data["received_at"]

    # Дедупликация: проверяем не обработали ли уже это письмо
    existing = db.query(SupplierResponse).filter(
        SupplierResponse.email_message_id == message_id
    ).first()
    if existing:
        logger.debug(f"[Email] Письмо {message_id} уже обработано (response_id={existing.id}), пропускаю")
        return 0

    # Извлекаем текст из вложений (OCR)
    ocr_texts = []
    primary_file_path = None
    primary_file_name = None

    for att in attachments:
        try:
            text = extract_text(att["path"])
            if text:
                ocr_texts.append(f"[Вложение: {att['filename']}]\n{text}")
            if primary_file_path is None:
                primary_file_path = att["path"]
                primary_file_name = att["filename"]
            # Пытаемся найти номер претензии в тексте вложения
            if not claim_number and text:
                claim_number = extract_claim_number_from_text(text)
        except Exception as e:
            logger.warning(f"[Email] Ошибка OCR вложения {att['filename']}: {e}")

    # Формируем финальный текст для LLM
    # Порядок: тело письма + OCR вложений (вложение приоритетнее)
    combined_text_parts = []
    if body_text and body_text.strip():
        combined_text_parts.append(f"[Текст письма]\n{body_text.strip()}")
    if ocr_texts:
        combined_text_parts.extend(ocr_texts)

    full_text = "\n\n" + "\n\n───\n\n".join(combined_text_parts) if combined_text_parts else ""

    if not full_text.strip():
        logger.warning(f"[Email] Письмо {message_id} — нет извлечённого текста, пропускаю")
        return 0

    # Ищем претензию в БД
    claim = None
    if claim_number:
        claim = db.query(Claim).filter(Claim.claim_number == claim_number).first()

    # Если не найдена — пробуем ERP
    if not claim and claim_number:
        try:
            erp_data = run_async(erp_client.get_claim(claim_number))
            if erp_data:
                from main import _create_claim_from_erp
                claim = _create_claim_from_erp(erp_data, db)
                logger.info(f"[Email] Претензия {claim_number} создана из ERP")
        except Exception as e:
            logger.warning(f"[Email] Не удалось получить претензию {claim_number} из ERP: {e}")

    if not claim:
        # Создаём ответ без привязки к претензии — специалист разберёт вручную
        logger.warning(
            f"[Email] Претензия не определена для письма от {email_data.get('sender')}. "
            f"Тема: {email_data.get('subject', '')[:80]}. Будет создан ответ без привязки."
        )

    # Создаём запись SupplierResponse
    response = SupplierResponse(
        claim_id           = claim.id if claim else None,
        response_type      = response_type,
        raw_text           = full_text[:50000],   # Ограничение на размер
        file_path          = primary_file_path,
        file_original_name = primary_file_name,
        received_at        = received_at,
        submitted_by       = submitted_by,
        email_message_id   = message_id,
        email_sender       = email_data.get("sender", ""),
        email_subject      = email_data.get("subject", "")[:500],
        processing_status  = "pending",
    )
    db.add(response)
    db.flush()

    if claim:
        claim.status = "response_received"

    db.flush()

    # Ставим в очередь на LLM-анализ
    if claim:
        process_task.delay(response.id)
        logger.info(
            f"[Email] Поставлен в очередь response_id={response.id}, "
            f"претензия={claim_number}, тип={response_type}"
        )
    else:
        # Без привязки к претензии — отправляем в закупки на ручной разбор
        _notify_unmatched_email(email_data, response.id)
        logger.warning(f"[Email] Ответ response_id={response.id} без претензии — уведомление в закупки")

    return 1


def _notify_unmatched_email(email_data: dict, response_id: int):
    """Уведомляет закупки о письме, которое не удалось привязать к претензии."""
    import httpx
    from config import settings

    if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_PROCUREMENT_CHAT_ID:
        return

    text = (
        f"⚠️ *Письмо без привязки к претензии*\n\n"
        f"От: `{email_data.get('sender', 'N/A')}`\n"
        f"Тема: {email_data.get('subject', 'N/A')[:100]}\n"
        f"Вложения: {len(email_data.get('attachments', []))} шт.\n\n"
        f"Номер претензии не определён автоматически.\n"
        f"ID ответа: `{response_id}` — требует ручной привязки."
    )

    try:
        httpx.post(
            f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id":    settings.TELEGRAM_PROCUREMENT_CHAT_ID,
                "text":       text,
                "parse_mode": "Markdown",
            },
            timeout=10.0,
        )
    except Exception as e:
        logger.warning(f"[Email] Ошибка Telegram уведомления: {e}")
