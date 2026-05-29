"""
Supplier Response Management — FastAPI Backend
"""
import logging
import os
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Depends, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.orm import Session

from config import settings
from database import get_db, engine
from models import Base, Claim, ClaimItem, SupplierResponse, Supplier, Contract, AuditLog
from services.ocr import extract_text, extract_claim_number_from_text
from services.rag import index_contract
from services.erp import erp_client

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

# Создаём таблицы (если нет)
Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="Supplier Response Management",
    description="ИИ-инструмент анализа ответов поставщиков на претензии о недопоставке",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Раздаём фронтенд
if Path("/app/frontend/static").exists():
    app.mount("/static", StaticFiles(directory="/app/frontend/static"), name="static")


# ─────────────────────────────────────────────────────────────────────────────
# HEALTH CHECK
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "model": settings.OLLAMA_MODEL}


# ─────────────────────────────────────────────────────────────────────────────
# CLAIMS — Претензии
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/claims")
def list_claims(
    status: Optional[str] = None,
    supplier_id: Optional[int] = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db)
):
    """Список претензий с фильтрацией."""
    query = db.query(Claim)
    if status:
        query = query.filter(Claim.status == status)
    if supplier_id:
        query = query.filter(Claim.supplier_id == supplier_id)

    total = query.count()
    claims = query.order_by(Claim.updated_at.desc()).offset(offset).limit(limit).all()

    result = []
    for claim in claims:
        supplier = db.query(Supplier).filter(Supplier.id == claim.supplier_id).first()
        last_response = (
            db.query(SupplierResponse)
            .filter(SupplierResponse.claim_id == claim.id)
            .order_by(SupplierResponse.created_at.desc())
            .first()
        )
        result.append({
            "id": claim.id,
            "claim_number": claim.claim_number,
            "order_number": claim.order_number,
            "supplier_name": supplier.name if supplier else None,
            "total_penalty_amount": float(claim.total_penalty_amount or 0),
            "currency": claim.currency,
            "status": claim.status,
            "planned_delivery_date": str(claim.planned_delivery_date) if claim.planned_delivery_date else None,
            "actual_delivery_date": str(claim.actual_delivery_date) if claim.actual_delivery_date else None,
            "created_at": claim.created_at.isoformat() if claim.created_at else None,
            "last_response": {
                "classification": last_response.classification,
                "recommendation": last_response.recommendation,
                "risk_level": last_response.risk_level,
                "processing_status": last_response.processing_status,
            } if last_response else None,
        })

    return {"total": total, "items": result}


@app.get("/api/claims/{claim_number}")
def get_claim(claim_number: str, db: Session = Depends(get_db)):
    """Полные данные претензии с позициями и ответами."""
    claim = db.query(Claim).filter(Claim.claim_number == claim_number).first()
    if not claim:
        raise HTTPException(status_code=404, detail=f"Претензия {claim_number} не найдена")

    supplier = db.query(Supplier).filter(Supplier.id == claim.supplier_id).first()
    items = db.query(ClaimItem).filter(ClaimItem.claim_id == claim.id).all()
    responses = (
        db.query(SupplierResponse)
        .filter(SupplierResponse.claim_id == claim.id)
        .order_by(SupplierResponse.created_at.desc())
        .all()
    )

    return {
        "id": claim.id,
        "claim_number": claim.claim_number,
        "order_number": claim.order_number,
        "supplier": {"id": supplier.id, "name": supplier.name, "inn": supplier.inn} if supplier else None,
        "planned_delivery_date": str(claim.planned_delivery_date) if claim.planned_delivery_date else None,
        "actual_delivery_date": str(claim.actual_delivery_date) if claim.actual_delivery_date else None,
        "total_penalty_amount": float(claim.total_penalty_amount or 0),
        "currency": claim.currency,
        "status": claim.status,
        "items": [
            {
                "sku": i.sku,
                "product_name": i.product_name,
                "ordered_qty": float(i.ordered_qty or 0),
                "received_qty": float(i.received_qty or 0),
                "shortage_qty": float(i.shortage_qty or 0),
                "penalized_qty": float(i.penalized_qty or 0),
                "unit_price": float(i.unit_price or 0),
                "penalty_rate_pct": float(i.penalty_rate_pct or 0),
                "penalty_amount": float(i.penalty_amount or 0),
            }
            for i in items
        ],
        "responses": [_serialize_response(r) for r in responses],
    }


@app.post("/api/claims/sync")
async def sync_claims_from_erp(db: Session = Depends(get_db)):
    """Ручной запуск синхронизации претензий из ERP."""
    from workers.tasks import sync_erp_claims
    task = sync_erp_claims.delay()
    return {"task_id": str(task.id), "status": "queued"}


# ─────────────────────────────────────────────────────────────────────────────
# RESPONSES — Загрузка и обработка ответов поставщиков
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/api/responses/upload")
async def upload_supplier_response(
    file: UploadFile = File(...),
    claim_number: Optional[str] = Form(None),
    response_type: str = Form("manual_upload"),
    submitted_by: str = Form("web_ui"),
    db: Session = Depends(get_db)
):
    """
    Загрузка ответа поставщика (PDF, JPG, PNG).
    Если claim_number не указан — пытается определить автоматически из текста.
    """
    # Валидация файла
    allowed_ext = {".pdf", ".jpg", ".jpeg", ".png", ".tiff", ".tif", ".txt"}
    file_ext = Path(file.filename).suffix.lower()
    if file_ext not in allowed_ext:
        raise HTTPException(status_code=400, detail=f"Неподдерживаемый формат: {file_ext}")

    max_size = settings.UPLOAD_MAX_SIZE_MB * 1024 * 1024
    content = await file.read()
    if len(content) > max_size:
        raise HTTPException(status_code=400, detail=f"Файл превышает {settings.UPLOAD_MAX_SIZE_MB} MB")

    # Сохраняем файл
    file_id = str(uuid.uuid4())
    save_path = os.path.join(settings.UPLOAD_DIR, "responses", f"{file_id}{file_ext}")
    os.makedirs(os.path.dirname(save_path), exist_ok=True)
    with open(save_path, "wb") as f:
        f.write(content)

    # OCR
    try:
        raw_text = extract_text(save_path)
    except Exception as e:
        logger.error(f"OCR error: {e}")
        raw_text = ""

    # Определяем номер претензии
    if not claim_number and raw_text:
        claim_number = extract_claim_number_from_text(raw_text)

    if not claim_number:
        raise HTTPException(
            status_code=422,
            detail="Номер претензии не указан и не найден в документе. Укажите claim_number вручную."
        )

    # Ищем претензию в БД
    claim = db.query(Claim).filter(Claim.claim_number == claim_number).first()
    if not claim:
        # Пробуем получить из ERP
        try:
            erp_data = await erp_client.get_claim(claim_number)
            if erp_data:
                claim = _create_claim_from_erp(erp_data, db)
            else:
                raise HTTPException(status_code=404, detail=f"Претензия {claim_number} не найдена в БД и ERP")
        except Exception as e:
            raise HTTPException(status_code=404, detail=f"Претензия {claim_number} не найдена: {e}")

    # Создаём запись ответа
    response = SupplierResponse(
        claim_id           = claim.id,
        response_type      = response_type,
        raw_text           = raw_text,
        file_path          = save_path,
        file_original_name = file.filename,
        submitted_by       = submitted_by,
        processing_status  = "pending",
    )
    db.add(response)
    claim.status = "response_received"
    db.commit()
    db.refresh(response)

    # Запускаем асинхронный анализ
    from workers.tasks import process_supplier_response
    task = process_supplier_response.delay(response.id)

    logger.info(f"Загружен ответ по претензии {claim_number}, response_id={response.id}, task_id={task.id}")

    return {
        "response_id": response.id,
        "claim_number": claim_number,
        "task_id": str(task.id),
        "status": "queued",
        "message": "Файл принят, анализ запущен. Используйте /api/responses/{id}/status для проверки."
    }


@app.get("/api/responses/{response_id}")
def get_response(response_id: int, db: Session = Depends(get_db)):
    """Результаты анализа конкретного ответа."""
    response = db.query(SupplierResponse).filter(SupplierResponse.id == response_id).first()
    if not response:
        raise HTTPException(status_code=404, detail="Ответ не найден")
    return _serialize_response(response, include_brief=True)


@app.get("/api/responses/{response_id}/status")
def get_response_status(response_id: int, db: Session = Depends(get_db)):
    """Быстрая проверка статуса обработки."""
    response = db.query(SupplierResponse).filter(SupplierResponse.id == response_id).first()
    if not response:
        raise HTTPException(status_code=404, detail="Ответ не найден")
    return {
        "response_id": response_id,
        "processing_status": response.processing_status,
        "classification": response.classification,
        "recommendation": response.recommendation,
        "processed_at": response.processed_at.isoformat() if response.processed_at else None,
        "error": response.processing_error,
    }


@app.get("/api/responses/{response_id}/brief")
def get_legal_brief(response_id: int, db: Session = Depends(get_db)):
    """Получить справку для юриста."""
    response = db.query(SupplierResponse).filter(SupplierResponse.id == response_id).first()
    if not response:
        raise HTTPException(status_code=404, detail="Ответ не найден")
    if response.processing_status != "done":
        raise HTTPException(status_code=425, detail=f"Анализ ещё не завершён: {response.processing_status}")
    return {
        "response_id": response_id,
        "legal_brief": response.legal_brief,
        "classification": response.classification,
        "recommendation": response.recommendation,
        "risk_level": response.risk_level,
    }


# ─────────────────────────────────────────────────────────────────────────────
# CONTRACTS — Загрузка договоров
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/api/contracts/upload")
async def upload_contract(
    file: UploadFile = File(...),
    supplier_id: int = Form(...),
    contract_number: str = Form(...),
    doc_type: str = Form("main"),
    valid_from: Optional[str] = Form(None),
    valid_to: Optional[str] = Form(None),
    db: Session = Depends(get_db)
):
    """Загрузка договора / доп.соглашения в ChromaDB для поиска."""
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Только PDF")

    supplier = db.query(Supplier).filter(Supplier.id == supplier_id).first()
    if not supplier:
        raise HTTPException(status_code=404, detail=f"Поставщик id={supplier_id} не найден")

    # Сохраняем PDF
    file_id = str(uuid.uuid4())
    save_path = os.path.join(settings.UPLOAD_DIR, "contracts", f"{file_id}.pdf")
    os.makedirs(os.path.dirname(save_path), exist_ok=True)

    content = await file.read()
    with open(save_path, "wb") as f:
        f.write(content)

    # Индексируем в ChromaDB
    try:
        chroma_doc_id = await index_contract(
            file_path=save_path,
            supplier_id=supplier_id,
            supplier_name=supplier.name,
            contract_number=contract_number,
            doc_type=doc_type,
        )
    except Exception as e:
        os.remove(save_path)
        raise HTTPException(status_code=500, detail=f"Ошибка индексации договора: {e}")

    # Сохраняем в БД
    contract = Contract(
        supplier_id     = supplier_id,
        contract_number = contract_number,
        doc_type        = doc_type,
        file_path       = save_path,
        chroma_doc_id   = chroma_doc_id,
        valid_from      = valid_from,
        valid_to        = valid_to,
    )
    db.add(contract)
    db.commit()

    return {
        "contract_id": contract.id,
        "supplier_id": supplier_id,
        "contract_number": contract_number,
        "chroma_doc_id": chroma_doc_id,
        "status": "indexed",
    }


# ─────────────────────────────────────────────────────────────────────────────
# SUPPLIERS
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/suppliers")
def list_suppliers(db: Session = Depends(get_db)):
    suppliers = db.query(Supplier).order_by(Supplier.name).all()
    return [{"id": s.id, "inn": s.inn, "name": s.name} for s in suppliers]


@app.post("/api/suppliers")
def create_supplier(name: str = Form(...), inn: Optional[str] = Form(None), db: Session = Depends(get_db)):
    supplier = Supplier(name=name, inn=inn)
    db.add(supplier)
    db.commit()
    db.refresh(supplier)
    return {"id": supplier.id, "name": supplier.name, "inn": supplier.inn}


# ─────────────────────────────────────────────────────────────────────────────
# DASHBOARD STATS
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/stats")
def get_stats(db: Session = Depends(get_db)):
    """Сводная статистика для дашборда."""
    from sqlalchemy import func as sa_func

    total_claims = db.query(Claim).count()
    pending = db.query(Claim).filter(Claim.status == "pending").count()
    processed = db.query(Claim).filter(Claim.status == "processed").count()
    to_legal = db.query(SupplierResponse).filter(
        SupplierResponse.recommendation == "TRANSFER_TO_LEGAL",
        SupplierResponse.processing_status == "done"
    ).count()

    total_penalty = db.query(sa_func.sum(Claim.total_penalty_amount)).scalar() or 0

    class_stats = (
        db.query(SupplierResponse.classification, sa_func.count())
        .filter(SupplierResponse.processing_status == "done")
        .group_by(SupplierResponse.classification)
        .all()
    )

    return {
        "total_claims": total_claims,
        "pending": pending,
        "processed": processed,
        "ready_for_legal": to_legal,
        "total_penalty_byn": float(total_penalty),
        "classifications": {k: v for k, v in class_stats if k},
    }


# ─────────────────────────────────────────────────────────────────────────────
# FRONTEND ROUTE
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/")
def frontend():
    index = Path("/app/frontend/static/index.html")
    if index.exists():
        return FileResponse(str(index))
    return JSONResponse({"message": "Supplier Response Management API", "docs": "/docs"})


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _serialize_response(r: SupplierResponse, include_brief: bool = False) -> dict:
    result = {
        "id": r.id,
        "claim_id": r.claim_id,
        "response_type": r.response_type,
        "file_original_name": r.file_original_name,
        "received_at": r.received_at.isoformat() if r.received_at else None,
        "submitted_by": r.submitted_by,
        "classification": r.classification,
        "supplier_position": r.supplier_position,
        "discrepancies": r.discrepancies,
        "legal_basis_assessment": r.legal_basis_assessment,
        "recommendation": r.recommendation,
        "risk_level": r.risk_level,
        "llm_notes": r.llm_notes,
        "processing_status": r.processing_status,
        "processing_error": r.processing_error,
        "processed_at": r.processed_at.isoformat() if r.processed_at else None,
    }
    if include_brief:
        result["legal_brief"] = r.legal_brief
    return result


def _create_claim_from_erp(erp_data: dict, db: Session) -> Claim:
    """Создаёт претензию в БД из данных ERP."""
    supplier_name = erp_data.get("supplier_name", "")
    supplier_inn  = erp_data.get("supplier_inn")

    supplier = None
    if supplier_inn:
        supplier = db.query(Supplier).filter(Supplier.inn == supplier_inn).first()
    if not supplier and supplier_name:
        supplier = db.query(Supplier).filter(Supplier.name == supplier_name).first()
    if not supplier and supplier_name:
        supplier = Supplier(name=supplier_name, inn=supplier_inn)
        db.add(supplier)
        db.flush()

    claim = Claim(
        claim_number          = erp_data["claim_number"],
        order_number          = erp_data.get("order_number", ""),
        supplier_id           = supplier.id if supplier else None,
        total_penalty_amount  = erp_data.get("total_penalty_amount"),
        currency              = erp_data.get("currency", "BYN"),
        erp_raw               = erp_data,
        erp_synced_at         = datetime.utcnow(),
    )

    if erp_data.get("planned_delivery_date"):
        from datetime import date
        try:
            claim.planned_delivery_date = date.fromisoformat(erp_data["planned_delivery_date"])
        except ValueError:
            pass
    if erp_data.get("actual_delivery_date"):
        from datetime import date
        try:
            claim.actual_delivery_date = date.fromisoformat(erp_data["actual_delivery_date"])
        except ValueError:
            pass

    db.add(claim)
    db.flush()

    for item_data in erp_data.get("items", []):
        db.add(ClaimItem(
            claim_id              = claim.id,
            sku                   = item_data.get("sku", ""),
            product_name          = item_data.get("product_name", ""),
            ordered_qty           = item_data.get("ordered_qty"),
            received_qty          = item_data.get("received_qty"),
            promo_qty             = item_data.get("promo_qty", 0),
            allowed_deviation_pct = item_data.get("allowed_deviation_pct", 0),
            shortage_qty          = item_data.get("shortage_qty"),
            penalized_qty         = item_data.get("penalized_qty"),
            unit_price            = item_data.get("unit_price"),
            penalty_rate_pct      = item_data.get("penalty_rate_pct"),
            penalty_amount        = item_data.get("penalty_amount"),
        ))

    db.commit()
    return claim
