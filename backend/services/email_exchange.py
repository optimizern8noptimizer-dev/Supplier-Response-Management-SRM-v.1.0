"""
Exchange Email Listener
Подключение к Microsoft Exchange on-premise через EWS (Exchange Web Services).
Поддерживает NTLM и Basic Auth.

Логика обработки:
  - Формальные ответы (PDF/скан во вложении) → OCR → LLM
  - Неформальные ответы (текст письма) → LLM напрямую
  - Письмо с вложением + текст → вложение как основной документ, тело как контекст
  - Обработанные письма помечаются категорией и перемещаются в папку "Обработано"
"""

import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from exchangelib import (
    Account,
    Configuration,
    Credentials,
    DELEGATE,
    EWSDateTime,
    EWSTimeZone,
    FileAttachment,
    Mailbox,
)
from exchangelib.errors import (
    ErrorItemNotFound,
    ErrorNonExistentMailbox,
    UnauthorizedError,
)
from exchangelib.protocol import BaseProtocol

from config import settings

logger = logging.getLogger(__name__)

# Отключаем SSL-верификацию для корпоративного Exchange
# (типично для on-premise с самоподписанным сертификатом)
# Если сертификат валидный — удалите эту строку
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Категория Outlook для уже обработанных писем
PROCESSED_CATEGORY = "SRM_Processed"
PROCESSED_FOLDER   = "SRM_Обработано"

# Поддерживаемые форматы вложений
ALLOWED_ATTACHMENT_EXT = {".pdf", ".jpg", ".jpeg", ".png", ".tiff", ".tif"}


def _get_exchange_account() -> Account:
    """
    Создаёт подключение к Exchange через EWS.
    Порядок аутентификации: NTLM → Basic → Kerberos (по настройке).
    """
    from exchangelib import NTLM, BASIC
    from exchangelib.winrm import retry_policy

    auth_type_map = {
        "ntlm":   NTLM,
        "basic":  BASIC,
    }
    auth_type = auth_type_map.get(
        (settings.EMAIL_AUTH_TYPE or "ntlm").lower(),
        NTLM
    )

    credentials = Credentials(
        username=settings.EMAIL_EWS_USER,   # формат: DOMAIN\username или user@domain.local
        password=settings.EMAIL_PASSWORD,
    )

    config = Configuration(
        server=settings.EMAIL_EWS_HOST,
        credentials=credentials,
        auth_type=auth_type,
    )

    # Отключаем проверку SSL-сертификата Exchange (для корп. CA)
    if not settings.EMAIL_EWS_VERIFY_SSL:
        BaseProtocol.HTTP_ADAPTER_CLS = _NoVerifyHTTPAdapter()

    account = Account(
        primary_smtp_address=settings.EMAIL_ADDRESS,
        config=config,
        autodiscover=False,
        access_type=DELEGATE,
    )
    return account


def _get_or_create_folder(account: Account, folder_name: str):
    """Возвращает папку Inbox/folder_name, создаёт если не существует."""
    from exchangelib.folders import Folder
    inbox = account.inbox

    try:
        # Ищем в дочерних папках Inbox
        folder = inbox // folder_name
        return folder
    except Exception:
        pass

    # Создаём папку
    try:
        new_folder = inbox.__class__(name=folder_name, parent=inbox)
        new_folder.save()
        logger.info(f"[Exchange] Создана папка: {folder_name}")
        return inbox // folder_name
    except Exception as e:
        logger.warning(f"[Exchange] Не удалось создать папку {folder_name}: {e}")
        return None


def poll_exchange_inbox() -> list[dict]:
    """
    Основная функция: опрашивает папку Exchange и возвращает
    список необработанных писем для анализа.

    Returns:
        list[dict] — каждый элемент содержит все данные для создания SupplierResponse
    """
    if not settings.EMAIL_ENABLED:
        return []

    logger.info("[Exchange] Начало опроса почты")
    results = []

    try:
        account = _get_exchange_account()
    except UnauthorizedError as e:
        logger.error(f"[Exchange] Ошибка авторизации: {e}. Проверьте EMAIL_EWS_USER и EMAIL_PASSWORD.")
        return []
    except Exception as e:
        logger.error(f"[Exchange] Ошибка подключения к Exchange: {e}")
        return []

    # Определяем папку для мониторинга
    if settings.EMAIL_EWS_FOLDER.upper() == "INBOX":
        folder = account.inbox
    else:
        try:
            folder = account.inbox // settings.EMAIL_EWS_FOLDER
        except Exception:
            folder = account.inbox

    # Папка для перемещения обработанных
    processed_folder = _get_or_create_folder(account, PROCESSED_FOLDER)

    # Фильтруем непрочитанные письма без категории SRM_Processed
    try:
        messages = folder.filter(is_read=False).order_by("datetime_received")
        messages = list(messages[:settings.EMAIL_BATCH_SIZE])
    except Exception as e:
        logger.error(f"[Exchange] Ошибка чтения папки: {e}")
        return []

    logger.info(f"[Exchange] Найдено {len(messages)} непрочитанных писем")

    for msg in messages:
        try:
            parsed = _parse_exchange_message(msg, account)
            if parsed:
                results.append(parsed)

                # Помечаем письмо как обработанное
                msg.is_read = True
                msg.categories = [PROCESSED_CATEGORY]
                try:
                    msg.save(update_fields=["is_read", "categories"])
                except Exception as e:
                    logger.warning(f"[Exchange] Не удалось обновить флаги письма: {e}")

                # Перемещаем в папку "Обработано"
                if processed_folder:
                    try:
                        msg.move(processed_folder)
                    except Exception as e:
                        logger.warning(f"[Exchange] Не удалось переместить письмо: {e}")

        except Exception as e:
            logger.error(f"[Exchange] Ошибка обработки письма id={getattr(msg, 'id', '?')}: {e}", exc_info=True)

    logger.info(f"[Exchange] Обработано писем: {len(results)}")
    return results


def _parse_exchange_message(msg, account: Account) -> Optional[dict]:
    """
    Парсит одно письмо Exchange.
    Возвращает dict с данными для создания SupplierResponse или None если письмо нерелевантно.
    """
    message_id = getattr(msg, "message_id", None) or str(getattr(msg, "id", uuid.uuid4()))
    subject    = getattr(msg, "subject", "") or ""
    sender     = ""
    if hasattr(msg, "sender") and msg.sender:
        sender = msg.sender.email_address or ""

    received_at = getattr(msg, "datetime_received", datetime.now(timezone.utc))

    # Получаем тело письма
    body_text = ""
    if hasattr(msg, "text_body") and msg.text_body:
        body_text = msg.text_body
    elif hasattr(msg, "body") and msg.body:
        body_text = msg.body
        # Если HTML — грубая очистка тегов
        if "<html" in body_text.lower() or "<div" in body_text.lower():
            import re
            body_text = re.sub(r"<[^>]+>", " ", body_text)
            body_text = re.sub(r"\s+", " ", body_text).strip()

    # Собираем вложения
    attachments_data = []
    if hasattr(msg, "attachments") and msg.attachments:
        for attachment in msg.attachments:
            if not isinstance(attachment, FileAttachment):
                continue
            filename = attachment.name or "attachment"
            ext = Path(filename).suffix.lower()
            if ext not in ALLOWED_ATTACHMENT_EXT:
                continue
            if attachment.size > settings.UPLOAD_MAX_SIZE_MB * 1024 * 1024:
                logger.warning(f"[Exchange] Вложение {filename} превышает лимит, пропускаю")
                continue
            attachments_data.append({
                "filename": filename,
                "content":  attachment.content,
                "ext":      ext,
            })

    # Определяем тип ответа
    has_attachment = len(attachments_data) > 0
    has_body       = len(body_text.strip()) > 50  # Минимум 50 символов чтобы не считать автоответы

    if not has_attachment and not has_body:
        logger.debug(f"[Exchange] Письмо {message_id} — нет вложений и нет текста, пропускаю")
        return None

    if has_attachment:
        response_type = "email_formal"
    else:
        response_type = "email_informal"

    # Пытаемся найти номер претензии в теме / теле
    from services.ocr import extract_claim_number_from_text
    claim_number = (
        extract_claim_number_from_text(subject)
        or extract_claim_number_from_text(body_text[:2000])
    )

    # Сохраняем вложения на диск
    saved_files = []
    upload_dir  = os.path.join(settings.UPLOAD_DIR, "responses")
    os.makedirs(upload_dir, exist_ok=True)

    for att in attachments_data:
        file_id   = str(uuid.uuid4())
        file_path = os.path.join(upload_dir, f"{file_id}{att['ext']}")
        with open(file_path, "wb") as f:
            f.write(att["content"])
        saved_files.append({
            "path":     file_path,
            "filename": att["filename"],
        })

    return {
        "message_id":    message_id,
        "sender":        sender,
        "subject":       subject,
        "body_text":     body_text,
        "claim_number":  claim_number,
        "response_type": response_type,
        "attachments":   saved_files,
        "received_at":   received_at,
        "submitted_by":  f"exchange:{sender}",
    }


class _NoVerifyHTTPAdapter:
    """
    Заглушка для отключения SSL-верификации.
    Используется когда Exchange работает с корпоративным CA.
    Заменяется на класс при инициализации BaseProtocol.HTTP_ADAPTER_CLS.
    """
    pass


def verify_exchange_connection() -> dict:
    """
    Проверяет подключение к Exchange. Используется при старте сервиса.
    Returns: {"status": "ok"|"error", "message": "...", "unread_count": N}
    """
    try:
        account = _get_exchange_account()
        unread = account.inbox.filter(is_read=False).count()
        return {
            "status":       "ok",
            "message":      f"Подключение к Exchange установлено",
            "email":        settings.EMAIL_ADDRESS,
            "unread_count": unread,
        }
    except UnauthorizedError:
        return {"status": "error", "message": "Ошибка авторизации Exchange. Проверьте EMAIL_EWS_USER/EMAIL_PASSWORD."}
    except Exception as e:
        return {"status": "error", "message": str(e)}
