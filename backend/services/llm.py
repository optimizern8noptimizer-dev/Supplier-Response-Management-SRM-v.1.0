"""
LLM Analysis Service
Ядро системы — анализ ответа поставщика через Ollama.
"""
import httpx
import json
import logging
from typing import Optional
from config import settings

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# ПРОМПТ 1: Классификация и структурированный анализ
# ─────────────────────────────────────────────────────────────────────────────
ANALYSIS_PROMPT = """Ты — старший юридический аналитик претензионного отдела крупной торговой компании.
Твоя задача: проанализировать ответ поставщика на претензию о недопоставке товара.

═══════════════════════════════════════════════════════
ДАННЫЕ ПРЕТЕНЗИИ ИЗ ERP-СИСТЕМЫ:
═══════════════════════════════════════════════════════
{claim_data}

═══════════════════════════════════════════════════════
ИЗВЛЕЧЁННЫЕ УСЛОВИЯ ДОГОВОРА:
═══════════════════════════════════════════════════════
{contract_excerpt}

═══════════════════════════════════════════════════════
ТЕКСТ ОТВЕТА ПОСТАВЩИКА:
═══════════════════════════════════════════════════════
{supplier_response}

═══════════════════════════════════════════════════════
ЗАДАНИЕ:
═══════════════════════════════════════════════════════

1. КЛАССИФИКАЦИЯ — выбери ОДНО значение:
   FULL_ACKNOWLEDGMENT    — поставщик полностью признаёт претензию, готов оплатить всю сумму штрафа
   PARTIAL_ACKNOWLEDGMENT — поставщик признаёт часть позиций или частичную сумму
   DENIAL                 — поставщик отклоняет претензию с конкретным обоснованием (форс-мажор, ошибка расчёта, условия договора)
   DENIAL_NO_BASIS        — поставщик отклоняет без обоснования или ссылается на несущественные причины
   COUNTER_CLAIM          — поставщик выдвигает встречное требование или зачёт
   DOCS_REQUEST           — поставщик запрашивает документы (ТТН, акты, накладные и т.д.) перед принятием решения
   DELAY_REQUEST          — поставщик просит отсрочку рассмотрения или оплаты
   MANUAL_REVIEW          — ответ содержит противоречивые или неоднозначные сведения, требует ручного разбора
   NO_RESPONSE            — ответ фактически отсутствует (прислан пустой бланк, автоответ и т.д.)

2. ПОЗИЦИЯ ПОСТАВЩИКА — изложи кратко (2-4 предложения) только то, что прямо написано в ответе.
   Не интерпретируй. Только факты из текста.

3. РАСХОЖДЕНИЯ — для каждой позиции, которую оспаривает поставщик:
   - что утверждает поставщик vs что зафиксировано в ERP
   - если поставщик не упоминает конкретные позиции — укажи "Поставщик не конкретизировал позиции"

4. ПРАВОВАЯ ОЦЕНКА — есть ли в ответе ссылки на:
   - конкретные пункты договора
   - нормы законодательства РБ
   - объективные обстоятельства (форс-мажор, вина третьих лиц)
   Оцени обоснованность: "обоснована" / "частично обоснована" / "не обоснована"

5. РЕКОМЕНДАЦИЯ — выбери ОДНО значение:
   TRANSFER_TO_LEGAL       — передать юристу без доработки (ответ однозначен)
   PROCUREMENT_REVIEW      — вернуть в закупки (нужна проверка данных или дополнительные документы)
   REQUEST_CLARIFICATION   — запросить уточнение у поставщика (ответ неполный или противоречивый)
   AWAIT_RESPONSE          — ответ не получен, инициировать повторный запрос

6. УРОВЕНЬ РИСКА для компании:
   HIGH   — поставщик имеет обоснованные возражения, высок риск проигрыша в суде
   MEDIUM — позиция поставщика частично обоснована или ответ неоднозначен
   LOW    — позиция компании сильная, ответ поставщика слабый или отсутствует

═══════════════════════════════════════════════════════
ФОРМАТ ОТВЕТА — СТРОГО JSON, без текста вне JSON:
═══════════════════════════════════════════════════════
{{
  "classification": "ЗНАЧЕНИЕ_ИЗ_СПИСКА",
  "supplier_position": "текст позиции поставщика 2-4 предложения",
  "discrepancies": [
    {{
      "sku": "артикул или null если поставщик не конкретизировал",
      "product_name": "название товара или null",
      "supplier_claim": "что утверждает поставщик по этой позиции",
      "erp_data": "что зафиксировано в ERP (факты из данных выше)",
      "gap_description": "описание расхождения между позицией поставщика и данными ERP"
    }}
  ],
  "legal_basis_assessment": "обоснована/частично обоснована/не обоснована: краткое пояснение одним предложением",
  "recommendation": "ЗНАЧЕНИЕ_ИЗ_СПИСКА",
  "risk_level": "HIGH/MEDIUM/LOW",
  "flags": ["список важных флагов если есть: COUNTER_CLAIM_AMOUNT, FORCE_MAJEURE_REFERENCED, CONTRACT_CLAUSE_CITED, PARTIAL_PAYMENT_OFFERED, DOCS_MISSING"],
  "notes": "любые детали которые юрист должен знать — ссылки на пункты договора в ответе поставщика, упомянутые суммы, даты, имена"
}}"""


# ─────────────────────────────────────────────────────────────────────────────
# ПРОМПТ 2: Генерация справки для юриста
# ─────────────────────────────────────────────────────────────────────────────
BRIEF_PROMPT = """Составь КРАТКУЮ СПРАВКУ ДЛЯ ЮРИСТА по претензии о недопоставке товара.
Стиль: деловой, чёткий, без воды. Объём: 250-350 слов.

═══════════════════════════════════════════════════════
ДАННЫЕ ПРЕТЕНЗИИ:
═══════════════════════════════════════════════════════
{claim_data}

═══════════════════════════════════════════════════════
РЕЗУЛЬТАТ АНАЛИЗА ОТВЕТА ПОСТАВЩИКА:
═══════════════════════════════════════════════════════
{analysis_json}

═══════════════════════════════════════════════════════
СТРУКТУРА СПРАВКИ (строго соблюдай):
═══════════════════════════════════════════════════════

СПРАВКА ПО ПРЕТЕНЗИИ №[номер]
Дата составления: [сегодня]

ПОСТАВЩИК: [наименование]
СУММА ПРЕТЕНЗИИ: [сумма] BYN
СТАТУС ОТВЕТА: [человекочитаемое название классификации]
УРОВЕНЬ РИСКА: [HIGH/MEDIUM/LOW с кратким пояснением]

ПОЗИЦИЯ ПОСТАВЩИКА:
[2-3 предложения — суть ответа поставщика]

КЛЮЧЕВЫЕ РАСХОЖДЕНИЯ:
[маркированный список расхождений между позицией поставщика и данными ERP]

ПРАВОВАЯ ОЦЕНКА ВОЗРАЖЕНИЙ ПОСТАВЩИКА:
[1-2 предложения — насколько обоснованы возражения]

РЕКОМЕНДУЕМЫЕ ДЕЙСТВИЯ:
[конкретные шаги — что делать дальше]

ПРИМЕЧАНИЯ ДЛЯ ЮРИСТА:
[важные детали — ссылки на договор, суммы встречных требований, запрошенные документы и т.д.]"""


# ─────────────────────────────────────────────────────────────────────────────
# Вспомогательные функции
# ─────────────────────────────────────────────────────────────────────────────

def format_claim_for_prompt(claim_data: dict) -> str:
    """Форматирует данные претензии из ERP в текст для промпта."""
    lines = [
        f"Претензия №: {claim_data.get('claim_number', 'N/A')}",
        f"Заказ №: {claim_data.get('order_number', 'N/A')}",
        f"Поставщик: {claim_data.get('supplier_name', 'N/A')}",
        f"Плановая дата поставки: {claim_data.get('planned_delivery_date', 'N/A')}",
        f"Фактическая дата поставки: {claim_data.get('actual_delivery_date', 'N/A')}",
        f"Причина претензии: Недопоставка товара",
        f"ИТОГОВАЯ СУММА ШТРАФА: {claim_data.get('total_penalty_amount', 'N/A')} BYN",
        "",
        "ПОЗИЦИИ ПРЕТЕНЗИИ (данные ERP):",
        "─" * 60,
    ]

    for i, item in enumerate(claim_data.get("items", []), 1):
        shortage = item.get('shortage_qty', 0)
        ordered  = item.get('ordered_qty', 0)
        received = item.get('received_qty', 0)
        pct_missing = round((float(shortage) / float(ordered) * 100), 1) if ordered else 0

        lines.extend([
            f"{i}. SKU {item.get('sku')} — {item.get('product_name')}",
            f"   Заказано: {ordered} | Принято: {received} | Недопоставлено: {shortage} ({pct_missing}%)",
            f"   Допустимое отклонение: {item.get('allowed_deviation_pct', 0)}%",
            f"   Штрафуемое количество: {item.get('penalized_qty')}",
            f"   Цена за ед.: {item.get('unit_price')} BYN | Ставка штрафа: {item.get('penalty_rate_pct')}%",
            f"   Сумма штрафа по позиции: {item.get('penalty_amount')} BYN",
            "─" * 60,
        ])

    return "\n".join(lines)


def _extract_json(text: str) -> dict:
    """Извлекает JSON из ответа LLM (может содержать текст вокруг)."""
    text = text.strip()
    # Убираем markdown-блоки если LLM их добавил
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text:
        text = text.split("```")[1].split("```")[0].strip()

    # Ищем JSON объект
    start = text.find("{")
    end   = text.rfind("}") + 1
    if start >= 0 and end > start:
        return json.loads(text[start:end])

    raise ValueError("JSON-объект не найден в ответе LLM")


async def _ollama_generate(prompt: str, temperature: float = 0.1) -> str:
    """Базовый вызов Ollama generate API."""
    async with httpx.AsyncClient(timeout=settings.OLLAMA_TIMEOUT_SECONDS) as client:
        response = await client.post(
            f"{settings.OLLAMA_URL}/api/generate",
            json={
                "model": settings.OLLAMA_MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": temperature,
                    "top_p": 0.9,
                    "num_ctx": 8192,
                    "repeat_penalty": 1.1,
                }
            }
        )
        response.raise_for_status()
        return response.json().get("response", "")


# ─────────────────────────────────────────────────────────────────────────────
# Главная функция анализа
# ─────────────────────────────────────────────────────────────────────────────

async def analyze_supplier_response(
    supplier_response_text: str,
    claim_data: dict,
    contract_excerpt: str = "Договор для данного поставщика не загружен в систему."
) -> dict:
    """
    Выполняет полный LLM-анализ ответа поставщика.

    Args:
        supplier_response_text: Извлечённый текст ответа (после OCR или из email)
        claim_data: Данные претензии из ERP (dict)
        contract_excerpt: Релевантный текст из договора (из ChromaDB)

    Returns:
        dict с полем 'legal_brief' и всеми полями анализа
    """
    claim_text = format_claim_for_prompt(claim_data)

    # ── Шаг 1: Структурированный анализ ──────────────────────────────────────
    logger.info(f"[LLM] Анализ ответа по претензии {claim_data.get('claim_number')}")

    analysis_prompt = ANALYSIS_PROMPT.format(
        claim_data=claim_text,
        contract_excerpt=contract_excerpt,
        supplier_response=supplier_response_text[:6000],  # Ограничение контекста
    )

    try:
        raw_analysis = await _ollama_generate(analysis_prompt, temperature=0.05)
        analysis = _extract_json(raw_analysis)
        logger.info(f"[LLM] Классификация: {analysis.get('classification')}")
    except (json.JSONDecodeError, ValueError) as e:
        logger.error(f"[LLM] Ошибка парсинга анализа: {e}. Raw: {raw_analysis[:300]}")
        # Fallback — отправляем на ручной разбор
        analysis = {
            "classification": "MANUAL_REVIEW",
            "supplier_position": f"Ошибка автоматического анализа. Требует ручного разбора.\nРаспознанный текст:\n{supplier_response_text[:500]}",
            "discrepancies": [],
            "legal_basis_assessment": "не обоснована: анализ не выполнен из-за ошибки LLM",
            "recommendation": "PROCUREMENT_REVIEW",
            "risk_level": "MEDIUM",
            "flags": [],
            "notes": f"LLM parse error: {str(e)}"
        }

    # ── Шаг 2: Генерация справки для юриста ──────────────────────────────────
    try:
        brief_prompt = BRIEF_PROMPT.format(
            claim_data=claim_text,
            analysis_json=json.dumps(analysis, ensure_ascii=False, indent=2),
        )
        legal_brief = await _ollama_generate(brief_prompt, temperature=0.2)
        analysis["legal_brief"] = legal_brief
    except Exception as e:
        logger.error(f"[LLM] Ошибка генерации справки: {e}")
        analysis["legal_brief"] = _build_fallback_brief(claim_data, analysis)

    analysis["llm_model"] = settings.OLLAMA_MODEL
    return analysis


def _build_fallback_brief(claim_data: dict, analysis: dict) -> str:
    """Формирует минимальную справку если LLM недоступен."""
    return (
        f"СПРАВКА ПО ПРЕТЕНЗИИ №{claim_data.get('claim_number', 'N/A')}\n"
        f"Поставщик: {claim_data.get('supplier_name', 'N/A')}\n"
        f"Сумма: {claim_data.get('total_penalty_amount', 'N/A')} BYN\n"
        f"Статус ответа: {analysis.get('classification', 'MANUAL_REVIEW')}\n"
        f"Рекомендация: {analysis.get('recommendation', 'PROCUREMENT_REVIEW')}\n\n"
        f"Позиция поставщика:\n{analysis.get('supplier_position', 'Требует ручного разбора')}\n\n"
        f"Примечание: автоматическая генерация справки не выполнена. "
        f"Документ требует ручного оформления."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Embedding для ChromaDB (договоры)
# ─────────────────────────────────────────────────────────────────────────────

async def get_embedding(text: str) -> list[float]:
    """Получает эмбеддинг текста через Ollama (модель nomic-embed-text)."""
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            f"{settings.OLLAMA_URL}/api/embeddings",
            json={
                "model": settings.OLLAMA_EMBED_MODEL,
                "prompt": text
            }
        )
        response.raise_for_status()
        return response.json()["embedding"]


# ─────────────────────────────────────────────────────────────────────────────
# Читаемые метки для UI/Telegram
# ─────────────────────────────────────────────────────────────────────────────

CLASSIFICATION_LABELS = {
    "FULL_ACKNOWLEDGMENT":    "✅ Полное признание",
    "PARTIAL_ACKNOWLEDGMENT": "🔶 Частичное признание",
    "DENIAL":                 "❌ Отказ с обоснованием",
    "DENIAL_NO_BASIS":        "❌ Отказ без обоснования",
    "COUNTER_CLAIM":          "⚠️ Встречное требование",
    "DOCS_REQUEST":           "📄 Запрос документов",
    "DELAY_REQUEST":          "⏳ Запрос отсрочки",
    "MANUAL_REVIEW":          "🔍 Требует ручного разбора",
    "NO_RESPONSE":            "📭 Ответ отсутствует",
}

RECOMMENDATION_LABELS = {
    "TRANSFER_TO_LEGAL":     "➡️ Передать юристу",
    "PROCUREMENT_REVIEW":    "🔄 Вернуть в закупки",
    "REQUEST_CLARIFICATION": "❓ Запросить уточнение",
    "AWAIT_RESPONSE":        "⏱️ Ожидать ответа",
}

RISK_LABELS = {
    "HIGH":   "🔴 Высокий",
    "MEDIUM": "🟡 Средний",
    "LOW":    "🟢 Низкий",
}
