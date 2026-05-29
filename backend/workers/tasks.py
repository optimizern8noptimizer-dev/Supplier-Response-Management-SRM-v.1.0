"""
Celery Tasks
Асинхронная обработка ответов поставщиков и периодические задачи.
"""
import asyncio
import logging
from datetime import datetime
from celery import Celery
from celery.schedules import crontab

from config import settings

logger = logging.getLogger(__name__)

celery_app = Celery(
    "srm_worker",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Europe/Minsk",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,  # Важно для долгих LLM задач
    task_soft_time_limit=300,
    task_time_limit=600,
)

# Периодические задачи
celery_app.conf.beat_schedule = {
    "sync-erp-claims": {
        "task": "workers.tasks.sync_erp_claims",
        "schedule": settings.ERP_SYNC_INTERVAL_MINUTES * 60,
    },
    "check-no-response-claims": {
        "task": "workers.tasks.check_no_response_claims",
        "schedule": crontab(hour="9", minute="0"),  # Ежедневно в 9:00
    },
}

# Регистрируем Email-задачу если включена
if settings.EMAIL_ENABLED:
    from workers.email_tasks import register_email_tasks
    register_email_tasks(celery_app, settings)


def run_async(coro):
    """Запускает async функцию в синхронном контексте Celery."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@celery_app.task(bind=True, name="workers.tasks.process_supplier_response", max_retries=2)
def process_supplier_response(self, response_id: int):
    """
    Основная задача: анализ ответа поставщика через LLM.
    Запускается после загрузки файла или получения email.
    """
    from database import get_db_session
    from models import SupplierResponse, Claim, ClaimItem, Supplier
    from services.llm import analyze_supplier_response
    from services.rag import search_contract_conditions, build_contract_search_query

    db = get_db_session()
    try:
        # Загружаем ответ из БД
        response = db.query(SupplierResponse).filter(SupplierResponse.id == response_id).first()
        if not response:
            logger.error(f"[Task] SupplierResponse id={response_id} не найден")
            return

        # Помечаем как "в обработке"
        response.processing_status = "processing"
        db.commit()

        # Загружаем претензию и её позиции
        claim = db.query(Claim).filter(Claim.id == response.claim_id).first()
        items = db.query(ClaimItem).filter(ClaimItem.claim_id == claim.id).all()
        supplier = db.query(Supplier).filter(Supplier.id == claim.supplier_id).first()

        # Формируем claim_data dict для промпта
        claim_data = {
            "claim_number":          claim.claim_number,
            "order_number":          claim.order_number,
            "supplier_name":         supplier.name if supplier else "Неизвестный поставщик",
            "planned_delivery_date": str(claim.planned_delivery_date) if claim.planned_delivery_date else "",
            "actual_delivery_date":  str(claim.actual_delivery_date) if claim.actual_delivery_date else "",
            "total_penalty_amount":  float(claim.total_penalty_amount or 0),
            "currency":              claim.currency or "BYN",
            "items": [
                {
                    "sku":                  item.sku,
                    "product_name":         item.product_name,
                    "ordered_qty":          float(item.ordered_qty or 0),
                    "received_qty":         float(item.received_qty or 0),
                    "promo_qty":            float(item.promo_qty or 0),
                    "allowed_deviation_pct": float(item.allowed_deviation_pct or 0),
                    "shortage_qty":         float(item.shortage_qty or 0),
                    "penalized_qty":        float(item.penalized_qty or 0),
                    "unit_price":           float(item.unit_price or 0),
                    "penalty_rate_pct":     float(item.penalty_rate_pct or 0),
                    "penalty_amount":       float(item.penalty_amount or 0),
                }
                for item in items
            ]
        }

        # Поиск условий договора
        supplier_id = supplier.id if supplier else 0
        contract_query = build_contract_search_query(claim_data)
        contract_excerpt = run_async(
            search_contract_conditions(supplier_id, contract_query)
        )

        # LLM анализ
        analysis = run_async(
            analyze_supplier_response(
                supplier_response_text=response.raw_text or "",
                claim_data=claim_data,
                contract_excerpt=contract_excerpt,
            )
        )

        # Сохраняем результаты
        response.classification         = analysis.get("classification")
        response.supplier_position      = analysis.get("supplier_position")
        response.discrepancies          = analysis.get("discrepancies", [])
        response.legal_basis_assessment = analysis.get("legal_basis_assessment")
        response.legal_brief            = analysis.get("legal_brief")
        response.recommendation         = analysis.get("recommendation")
        response.risk_level             = analysis.get("risk_level")
        response.llm_notes              = analysis.get("notes")
        response.llm_model              = analysis.get("llm_model")
        response.processing_status      = "done"
        response.processed_at           = datetime.utcnow()

        # Обновляем статус претензии
        claim.status = "processed"
        db.commit()

        logger.info(
            f"[Task] Обработан response_id={response_id}: "
            f"classification={response.classification}, "
            f"recommendation={response.recommendation}"
        )

        # Отправляем уведомление в Telegram
        send_telegram_notification.delay(response_id)

        return {
            "status": "done",
            "response_id": response_id,
            "classification": response.classification,
            "recommendation": response.recommendation,
        }

    except Exception as exc:
        logger.error(f"[Task] Ошибка обработки response_id={response_id}: {exc}", exc_info=True)
        if response:
            response.processing_status = "error"
            response.processing_error  = str(exc)[:1000]
            db.commit()

        # Retry через 60 секунд
        raise self.retry(exc=exc, countdown=60)

    finally:
        db.close()


@celery_app.task(name="workers.tasks.send_telegram_notification")
def send_telegram_notification(response_id: int):
    """Отправляет уведомление в Telegram после обработки ответа."""
    from database import get_db_session
    from models import SupplierResponse, Claim, Supplier
    from services.llm import CLASSIFICATION_LABELS, RECOMMENDATION_LABELS, RISK_LABELS
    import httpx

    db = get_db_session()
    try:
        response = db.query(SupplierResponse).filter(SupplierResponse.id == response_id).first()
        if not response or not settings.TELEGRAM_BOT_TOKEN:
            return

        claim = db.query(Claim).filter(Claim.id == response.claim_id).first()
        supplier = db.query(Supplier).filter(Supplier.id == claim.supplier_id).first() if claim else None

        # Определяем получателя
        recommendation = response.recommendation or ""
        if recommendation == "TRANSFER_TO_LEGAL" and settings.TELEGRAM_LEGAL_CHAT_ID:
            chat_id = settings.TELEGRAM_LEGAL_CHAT_ID
        elif recommendation in ("PROCUREMENT_REVIEW", "REQUEST_CLARIFICATION") and settings.TELEGRAM_PROCUREMENT_CHAT_ID:
            chat_id = settings.TELEGRAM_PROCUREMENT_CHAT_ID
        else:
            return

        # Формируем сообщение
        class_label = CLASSIFICATION_LABELS.get(response.classification, response.classification)
        recom_label  = RECOMMENDATION_LABELS.get(response.recommendation, response.recommendation)
        risk_label   = RISK_LABELS.get(response.risk_level, response.risk_level or "N/A")

        text = (
            f"📋 *Обработан ответ поставщика*\n\n"
            f"🔢 Претензия: `{claim.claim_number if claim else 'N/A'}`\n"
            f"🏢 Поставщик: {supplier.name if supplier else 'N/A'}\n"
            f"💰 Сумма штрафа: {claim.total_penalty_amount if claim else 'N/A'} BYN\n\n"
            f"*Статус ответа:* {class_label}\n"
            f"*Риск:* {risk_label}\n"
            f"*Рекомендация:* {recom_label}\n\n"
            f"Позиция поставщика:\n_{response.supplier_position[:300] if response.supplier_position else 'N/A'}_\n\n"
            f"[Открыть в системе](http://localhost:8000/responses/{response_id})"
        )

        httpx.post(
            f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
            timeout=10.0
        )

    except Exception as e:
        logger.error(f"[Task] Ошибка Telegram уведомления: {e}")
    finally:
        db.close()


@celery_app.task(name="workers.tasks.sync_erp_claims")
def sync_erp_claims():
    """
    Периодическая задача: синхронизация претензий из ERP Маркет.
    Запускается каждые ERP_SYNC_INTERVAL_MINUTES минут.
    """
    from database import get_db_session
    from models import Claim, ClaimItem, Supplier
    from services.erp import erp_client

    if not settings.ERP_API_URL:
        logger.debug("[Sync] ERP_API_URL не настроен, синхронизация пропущена")
        return {"synced": 0}

    db = get_db_session()
    synced = 0

    try:
        claims_data = run_async(erp_client.get_claims_batch(limit=200))

        for claim_data in claims_data:
            claim_number = claim_data.get("claim_number")
            if not claim_number:
                continue

            # Ищем или создаём поставщика
            supplier = None
            supplier_inn = claim_data.get("supplier_inn")
            if supplier_inn:
                supplier = db.query(Supplier).filter(Supplier.inn == supplier_inn).first()
            if not supplier:
                supplier = db.query(Supplier).filter(
                    Supplier.name == claim_data.get("supplier_name")
                ).first()
            if not supplier and claim_data.get("supplier_name"):
                supplier = Supplier(
                    inn=supplier_inn,
                    name=claim_data["supplier_name"]
                )
                db.add(supplier)
                db.flush()

            # Ищем или создаём претензию
            claim = db.query(Claim).filter(Claim.claim_number == claim_number).first()
            if not claim:
                claim = Claim(claim_number=claim_number)
                db.add(claim)

            claim.order_number          = claim_data.get("order_number", "")
            claim.supplier_id           = supplier.id if supplier else None
            claim.total_penalty_amount  = claim_data.get("total_penalty_amount")
            claim.currency              = claim_data.get("currency", "BYN")
            claim.erp_raw               = claim_data
            claim.erp_synced_at         = datetime.utcnow()
            db.flush()

            # Обновляем позиции
            db.query(ClaimItem).filter(ClaimItem.claim_id == claim.id).delete()
            for item_data in claim_data.get("items", []):
                db.add(ClaimItem(
                    claim_id             = claim.id,
                    sku                  = item_data.get("sku", ""),
                    product_name         = item_data.get("product_name", ""),
                    ordered_qty          = item_data.get("ordered_qty"),
                    received_qty         = item_data.get("received_qty"),
                    promo_qty            = item_data.get("promo_qty", 0),
                    allowed_deviation_pct = item_data.get("allowed_deviation_pct", 0),
                    shortage_qty         = item_data.get("shortage_qty"),
                    penalized_qty        = item_data.get("penalized_qty"),
                    unit_price           = item_data.get("unit_price"),
                    penalty_rate_pct     = item_data.get("penalty_rate_pct"),
                    penalty_amount       = item_data.get("penalty_amount"),
                ))
            synced += 1

        db.commit()
        logger.info(f"[Sync] ERP синхронизировано претензий: {synced}")
        return {"synced": synced}

    except Exception as e:
        db.rollback()
        logger.error(f"[Sync] Ошибка синхронизации ERP: {e}", exc_info=True)
        return {"error": str(e)}
    finally:
        db.close()


@celery_app.task(name="workers.tasks.check_no_response_claims")
def check_no_response_claims():
    """
    Ежедневная задача: находит претензии без ответа и уведомляет.
    """
    from database import get_db_session
    from models import Claim, Supplier
    from datetime import timedelta
    import httpx

    if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_PROCUREMENT_CHAT_ID:
        return

    db = get_db_session()
    try:
        # Претензии старше 5 дней без ответа
        cutoff_date = datetime.utcnow() - timedelta(days=5)
        pending_claims = db.query(Claim).filter(
            Claim.status == "pending",
            Claim.created_at < cutoff_date
        ).limit(20).all()

        if not pending_claims:
            return

        lines = [f"⏱️ *Претензии без ответа (>5 дней)*\n"]
        for claim in pending_claims:
            supplier = db.query(Supplier).filter(Supplier.id == claim.supplier_id).first()
            days_old = (datetime.utcnow() - claim.created_at).days
            lines.append(
                f"• №{claim.claim_number} | {supplier.name if supplier else 'N/A'} | "
                f"{claim.total_penalty_amount} BYN | {days_old} дн."
            )

        text = "\n".join(lines)
        httpx.post(
            f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": settings.TELEGRAM_PROCUREMENT_CHAT_ID,
                "text": text,
                "parse_mode": "Markdown",
            },
            timeout=10.0
        )

    except Exception as e:
        logger.error(f"[Task] Ошибка check_no_response: {e}")
    finally:
        db.close()
