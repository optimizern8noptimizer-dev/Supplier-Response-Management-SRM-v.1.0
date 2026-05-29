"""
RAG Service
Загрузка договоров в ChromaDB и поиск релевантных условий для анализа.
"""
import logging
import os
import hashlib
from pathlib import Path
from typing import Optional

import chromadb
from chromadb.config import Settings as ChromaSettings

from config import settings
from services.llm import get_embedding
from services.ocr import extract_text_from_pdf

logger = logging.getLogger(__name__)

COLLECTION_NAME = "supplier_contracts"
CHUNK_SIZE = 800       # символов на чанк
CHUNK_OVERLAP = 150    # перекрытие между чанками
TOP_K_RESULTS = 3      # количество релевантных фрагментов


def get_chroma_client() -> chromadb.HttpClient:
    return chromadb.HttpClient(
        host=settings.CHROMA_HOST,
        port=settings.CHROMA_PORT,
        settings=ChromaSettings(anonymized_telemetry=False)
    )


def get_collection():
    client = get_chroma_client()
    return client.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"}
    )


def _chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Разбивает текст на перекрывающиеся чанки."""
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        start += chunk_size - overlap
    return [c.strip() for c in chunks if c.strip()]


async def index_contract(
    file_path: str,
    supplier_id: int,
    supplier_name: str,
    contract_number: str,
    doc_type: str = "main"
) -> str:
    """
    Индексирует договор в ChromaDB.
    Возвращает doc_id для сохранения в БД.

    Args:
        file_path: Путь к PDF файлу договора
        supplier_id: ID поставщика в нашей БД
        supplier_name: Наименование поставщика
        contract_number: Номер договора
        doc_type: main | additional_agreement

    Returns:
        Базовый doc_id для всех чанков этого документа
    """
    logger.info(f"[RAG] Индексация договора {contract_number} ({doc_type}) для поставщика {supplier_id}")

    # Извлекаем текст
    text = extract_text_from_pdf(file_path)
    if not text or len(text) < 50:
        raise ValueError(f"Не удалось извлечь текст из договора: {file_path}")

    # Генерируем базовый doc_id на основе хэша файла
    file_hash = hashlib.md5(Path(file_path).read_bytes()).hexdigest()[:12]
    base_doc_id = f"contract_{supplier_id}_{file_hash}"

    # Разбиваем на чанки
    chunks = _chunk_text(text)
    logger.info(f"[RAG] Договор разбит на {len(chunks)} чанков")

    collection = get_collection()

    # Удаляем старые чанки этого документа (при переиндексации)
    try:
        existing = collection.get(where={"base_doc_id": base_doc_id})
        if existing["ids"]:
            collection.delete(ids=existing["ids"])
            logger.info(f"[RAG] Удалено {len(existing['ids'])} старых чанков")
    except Exception as e:
        logger.warning(f"[RAG] Ошибка при удалении старых чанков: {e}")

    # Получаем эмбеддинги и загружаем
    ids, embeddings, documents, metadatas = [], [], [], []

    for i, chunk in enumerate(chunks):
        chunk_id = f"{base_doc_id}_chunk_{i}"
        try:
            embedding = await get_embedding(chunk)
        except Exception as e:
            logger.error(f"[RAG] Ошибка получения эмбеддинга для чанка {i}: {e}")
            continue

        ids.append(chunk_id)
        embeddings.append(embedding)
        documents.append(chunk)
        metadatas.append({
            "base_doc_id":     base_doc_id,
            "supplier_id":     str(supplier_id),
            "supplier_name":   supplier_name,
            "contract_number": contract_number,
            "doc_type":        doc_type,
            "chunk_index":     i,
            "total_chunks":    len(chunks),
        })

    if not ids:
        raise RuntimeError("Ни один чанк не был проиндексирован")

    # Загружаем батчами по 100
    batch_size = 100
    for start in range(0, len(ids), batch_size):
        collection.add(
            ids=ids[start:start+batch_size],
            embeddings=embeddings[start:start+batch_size],
            documents=documents[start:start+batch_size],
            metadatas=metadatas[start:start+batch_size],
        )

    logger.info(f"[RAG] Проиндексировано {len(ids)} чанков для договора {contract_number}")
    return base_doc_id


async def search_contract_conditions(
    supplier_id: int,
    query: str,
    top_k: int = TOP_K_RESULTS
) -> str:
    """
    Ищет релевантные условия в договорах поставщика.

    Args:
        supplier_id: ID поставщика
        query: Поисковый запрос (обычно формируется из данных претензии)
        top_k: Количество возвращаемых фрагментов

    Returns:
        Конкатенированный текст релевантных фрагментов договора
    """
    collection = get_collection()

    # Проверяем есть ли договоры для поставщика
    try:
        existing = collection.get(where={"supplier_id": str(supplier_id)})
        if not existing["ids"]:
            return "Договор для данного поставщика не загружен в систему."
    except Exception:
        return "Договор для данного поставщика не загружен в систему."

    # Получаем эмбеддинг запроса
    query_embedding = await get_embedding(query)

    # Поиск
    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=top_k,
        where={"supplier_id": str(supplier_id)},
        include=["documents", "metadatas", "distances"]
    )

    if not results["documents"] or not results["documents"][0]:
        return "Релевантные условия договора не найдены."

    # Форматируем результат
    excerpts = []
    for doc, meta, dist in zip(
        results["documents"][0],
        results["metadatas"][0],
        results["distances"][0]
    ):
        relevance = round((1 - dist) * 100, 1)
        header = (
            f"[Договор {meta.get('contract_number')} "
            f"({meta.get('doc_type', 'main')}), "
            f"фрагмент {meta.get('chunk_index', 0)+1}/{meta.get('total_chunks', '?')}, "
            f"релевантность {relevance}%]"
        )
        excerpts.append(f"{header}\n{doc}")

    return "\n\n" + "\n\n─────\n\n".join(excerpts)


def build_contract_search_query(claim_data: dict) -> str:
    """
    Формирует поисковый запрос к договору на основе данных претензии.
    Ищет условия о штрафах, недопоставке, допустимых отклонениях.
    """
    return (
        "штраф недопоставка санкции ответственность поставщика "
        "допустимое отклонение процент штрафа порядок расчёта "
        "претензионный порядок сроки ответа на претензию"
    )
