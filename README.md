# 🤖 Supplier Response Management (SRM)

> ИИ-инструмент для автоматической обработки и классификации ответов поставщиков на претензии о недопоставке товара

[![Python](https://img.shields.io/badge/Python-3.11-blue?logo=python)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-green?logo=fastapi)](https://fastapi.tiangolo.com)
[![Docker](https://img.shields.io/badge/Docker-Compose-blue?logo=docker)](https://docs.docker.com/compose/)
[![Ollama](https://img.shields.io/badge/LLM-Ollama%20CPU-orange)](https://ollama.ai)
[![License](https://img.shields.io/badge/License-MIT-lightgrey)](LICENSE)

---

## Содержание

- [Что делает система](#что-делает-система)
- [Архитектура](#архитектура)
- [Требования](#требования)
- [Установка и запуск](#установка-и-запуск)
- [Конфигурация](#конфигурация)
- [Использование](#использование)
  - [Веб API](#веб-api)
  - [Telegram-бот](#telegram-бот)
  - [Мониторинг почты Exchange](#мониторинг-почты-exchange)
- [Классификация ответов](#классификация-ответов)
- [Интеграция с ERP](#интеграция-с-erp-маркет)
- [Загрузка договоров](#загрузка-договоров)
- [Устранение неисправностей](#устранение-неисправностей)
- [Управление сервисами](#управление-сервисами)

---

## Что делает система

**Задача:** ежедневно поступают сотни ответов поставщиков на претензии о недопоставке. Специалисты не успевают их обрабатывать — каждый ответ требует сопоставления с данными ERP, анализа договора и юридической оценки.

**Решение — система автоматически:**

1. Принимает ответы из почты Exchange, через веб-интерфейс или Telegram
2. Распознаёт отсканированные документы и PDF (OCR, Tesseract RUS+ENG)
3. Извлекает данные претензии из ERP по номеру
4. Ищет релевантные условия в базе договоров (RAG, ChromaDB)
5. Анализирует ответ через локальную LLM (Ollama) — **данные не покидают контур**
6. Классифицирует ответ по 9 категориям
7. Генерирует готовую справку для юриста
8. Уведомляет нужный отдел через Telegram

---

## Архитектура

```
┌──────────────────────────────────────────────────────────┐
│                   ИСТОЧНИКИ ОТВЕТОВ                       │
│  📧 Exchange EWS    📤 Web UI (API)    📱 Telegram Bot   │
└──────────────┬────────────┬────────────────┬─────────────┘
               │            │                │
               ▼            ▼                ▼
┌──────────────────────────────────────────────────────────┐
│                  FastAPI Backend                          │
│  OCR (Tesseract) → извлечение текста → номер претензии   │
└──────────────────────────┬───────────────────────────────┘
                           │
                           ▼ Celery Worker
┌───────────────┐  ┌───────────────┐  ┌────────────────────┐
│ ERP Маркет    │  │ ChromaDB RAG  │  │ Ollama (CPU)       │
│ REST API      │  │ Поиск условий │  │ qwen2.5:7b/14b     │
│ → позиции     │  │ договора      │  │ → классификация    │
│ → суммы штраф │  │ по supplier   │  │ → анализ позиций   │
└───────┬───────┘  └───────┬───────┘  │ → справка юристу  │
        └──────────────────┴──────────└────────────────────┘
                           │
                           ▼
              PostgreSQL (результаты)
                           │
               ┌───────────┴───────────┐
               ▼                       ▼
      Telegram → Юрист        Telegram → Закупки
   (TRANSFER_TO_LEGAL)     (PROCUREMENT_REVIEW)
```

### Стек

| Компонент       | Технология            | Версия   |
|----------------|-----------------------|----------|
| Backend API    | FastAPI + Uvicorn     | 0.115    |
| Очередь задач  | Celery + Redis        | 5.4      |
| База данных    | PostgreSQL            | 16       |
| Векторная БД   | ChromaDB              | 0.5.11   |
| LLM локальная  | Ollama                | latest   |
| OCR            | Tesseract + pdf2image | 5.x      |
| Email          | exchangelib (EWS)     | 5.4      |
| Telegram       | aiogram               | 3.14     |
| Контейнеры     | Docker Compose v2     | —        |

---

## Требования

### Сервер

| Параметр | Минимум        | Рекомендуется  |
|---------|----------------|----------------|
| RAM     | 8 GB           | 16+ GB         |
| CPU     | 4 ядра         | 8+ ядер        |
| Диск    | 20 GB SSD      | 50+ GB SSD     |
| ОС      | Ubuntu 22.04+  | Ubuntu 22.04   |
| GPU     | не требуется   | —              |

### Модели Ollama (выбрать по RAM)

| Модель          | RAM    | Скорость (8 CPU)       | Качество |
|----------------|--------|------------------------|----------|
| `qwen2.5:7b`  | 8 GB   | ~60–90 сек / документ | ★★★★☆   |
| `qwen2.5:14b` | 16 GB  | ~120–180 сек / документ | ★★★★★  |

### Внешние зависимости

- Docker 24.0+ и Docker Compose v2
- ERP Маркет с REST API и Bearer-токеном
- Microsoft Exchange on-premise (опционально, для мониторинга почты)
- Telegram Bot Token от [@BotFather](https://t.me/BotFather)

---

## Установка и запуск

### 1. Клонирование

```bash
git clone https://github.com/YOUR_ORG/supplier-response-management.git
cd supplier-response-management
```

### 2. Создание конфигурации

```bash
cp .env.example .env
nano .env
```

Минимально необходимые параметры:

```env
POSTGRES_PASSWORD=НадёжныйПароль2024!
OLLAMA_MODEL=qwen2.5:7b
TELEGRAM_BOT_TOKEN=токен_от_BotFather
TELEGRAM_ALLOWED_USERS=ваш_telegram_user_id
ERP_API_URL=https://market.company.by/api/v1
ERP_API_TOKEN=ваш_erp_токен
```

> Узнать свой Telegram user_id: написать боту [@userinfobot](https://t.me/userinfobot)

### 3. Запуск

```bash
chmod +x scripts/setup.sh
./scripts/setup.sh
```

Скрипт:
- Поднимает PostgreSQL, Redis, ChromaDB, Ollama
- Загружает LLM-модель (первый раз: 5–20 мин по скорости канала)
- Загружает модель эмбеддингов `nomic-embed-text` (~300 MB)
- Собирает и запускает backend, Celery, Telegram-бот

### 4. Проверка

```bash
# Все сервисы должны быть running
docker compose ps

# Ожидаемый ответ: {"status": "ok", "model": "qwen2.5:7b"}
curl http://localhost:8000/health

# Swagger UI с документацией всех API
xdg-open http://localhost:8000/docs
```

---

## Конфигурация

Все параметры задаются в файле `.env`. Файл не добавляется в git (включён в `.gitignore`).

### База данных

| Параметр            | Описание          | Пример                   |
|--------------------|-------------------|--------------------------|
| `POSTGRES_PASSWORD` | Пароль PostgreSQL | `StrongPass2024!`        |

### LLM (Ollama)

| Параметр                  | Описание                            | Значения                      |
|--------------------------|-------------------------------------|-------------------------------|
| `OLLAMA_MODEL`           | Модель для анализа                  | `qwen2.5:7b` / `qwen2.5:14b` |
| `OLLAMA_EMBED_MODEL`     | Модель эмбеддингов для RAG          | `nomic-embed-text`            |
| `OLLAMA_TIMEOUT_SECONDS` | Таймаут запроса (CPU медленный)    | `180`                         |

### ERP Маркет

| Параметр                    | Описание                     | Пример                             |
|----------------------------|------------------------------|------------------------------------|
| `ERP_API_URL`              | Базовый URL API              | `https://market.company.by/api/v1` |
| `ERP_API_TOKEN`            | Bearer-токен                 | `eyJhbGci...`                      |
| `ERP_SYNC_INTERVAL_MINUTES`| Интервал синхронизации (мин) | `30`                               |

### Telegram

| Параметр                       | Описание                             | Пример                |
|-------------------------------|--------------------------------------|-----------------------|
| `TELEGRAM_BOT_TOKEN`          | Токен от @BotFather                  | `1234567:ABC...`      |
| `TELEGRAM_ALLOWED_USERS`      | User ID через запятую                | `123456789,987654321` |
| `TELEGRAM_LEGAL_CHAT_ID`      | chat_id юридического отдела          | `-1001234567890`      |
| `TELEGRAM_PROCUREMENT_CHAT_ID`| chat_id отдела закупок               | `-1009876543210`      |

> Как получить chat_id группы: добавить бота в группу → написать `/start` →  
> `curl https://api.telegram.org/botТОКЕН/getUpdates` → найти `chat.id`

### Exchange / EWS

| Параметр                       | Описание                                    | Пример                 |
|-------------------------------|---------------------------------------------|------------------------|
| `EMAIL_ENABLED`               | Включить мониторинг Exchange                | `true` / `false`       |
| `EMAIL_ADDRESS`               | SMTP-адрес мониторируемого ящика            | `claims@company.by`    |
| `EMAIL_EWS_HOST`              | Хост Exchange (только hostname)             | `mail.company.by`      |
| `EMAIL_EWS_USER`              | Логин сервисного аккаунта                   | `COMPANY\svc_srm`      |
| `EMAIL_PASSWORD`              | Пароль сервисного аккаунта                  | `ServicePass2024!`     |
| `EMAIL_AUTH_TYPE`             | Метод авторизации                           | `ntlm` / `basic`       |
| `EMAIL_EWS_VERIFY_SSL`        | Проверять SSL (false для корп. CA)         | `false`                |
| `EMAIL_EWS_FOLDER`            | Папка для мониторинга                       | `INBOX`                |
| `EMAIL_CHECK_INTERVAL_SECONDS`| Интервал опроса Exchange (сек)             | `60`                   |
| `EMAIL_BATCH_SIZE`            | Максимум писем за один опрос               | `20`                   |

> Права для сервисного аккаунта: Exchange Admin Center → Recipients → Mailboxes →  
> `claims` → Mailbox delegation → Full Access → добавить `svc_srm`

---

## Использование

### Веб API

Полная документация всех endpoint'ов: **`http://localhost:8000/docs`**

#### Загрузить ответ поставщика

```bash
curl -X POST http://localhost:8000/api/responses/upload \
  -F "file=@supplier_response.pdf" \
  -F "claim_number=103827" \
  -F "submitted_by=Иванов И.И."
```

```json
{
  "response_id": 42,
  "claim_number": "103827",
  "task_id": "abc123-...",
  "status": "queued"
}
```

#### Проверить статус обработки

```bash
curl http://localhost:8000/api/responses/42/status
```

```json
{
  "processing_status": "done",
  "classification": "DENIAL",
  "recommendation": "TRANSFER_TO_LEGAL"
}
```

> `processing_status` принимает значения: `pending` → `processing` → `done` / `error`

#### Получить справку для юриста

```bash
curl http://localhost:8000/api/responses/42/brief
```

```json
{
  "classification": "DENIAL",
  "risk_level": "LOW",
  "recommendation": "TRANSFER_TO_LEGAL",
  "legal_brief": "СПРАВКА ПО ПРЕТЕНЗИИ №103827\n\nПОСТАВЩИК: ООО Рыбный Мир\n..."
}
```

#### Список претензий

```bash
# Все претензии (50 последних)
curl "http://localhost:8000/api/claims"

# Фильтр по статусу
curl "http://localhost:8000/api/claims?status=pending"

# Конкретная претензия с позициями и ответами
curl "http://localhost:8000/api/claims/103827"
```

#### Статистика

```bash
curl http://localhost:8000/api/stats
```

```json
{
  "total_claims": 1247,
  "pending": 83,
  "processed": 1164,
  "ready_for_legal": 312,
  "total_penalty_byn": 2847930.50,
  "classifications": {
    "FULL_ACKNOWLEDGMENT": 89,
    "DENIAL": 156,
    "DOCS_REQUEST": 67
  }
}
```

#### Ручная синхронизация из ERP

```bash
curl -X POST http://localhost:8000/api/claims/sync
```

---

### Telegram-бот

#### Команды

| Команда              | Действие                              |
|---------------------|---------------------------------------|
| `/start`            | Приветствие и список команд           |
| `/upload`           | Загрузить ответ поставщика (PDF/фото) |
| `/status 103827`    | Статус и результат анализа претензии  |
| `/stats`            | Сводная статистика системы            |
| `/help`             | Справка                               |

#### Сценарий загрузки

```
Специалист: /upload
Бот:        📎 Отправьте файл ответа поставщика (PDF или фото)

Специалист: [прикрепляет скан письма]
Бот:        ✅ Файл получен.
            Введите номер претензии или напишите "авто":

Специалист: 103827
Бот:        ✅ Файл принят на обработку
            Претензия: 103827 | ID ответа: 42
            Анализ запущен (~2-3 мин на CPU)

            [через 2-3 минуты — автоматически в чат юристов]

Система:    📋 Обработан ответ поставщика
            🔢 Претензия: 103827
            🏢 Поставщик: ООО Рыбный Мир
            💰 Сумма: 5 076.65 BYN
            Статус: ❌ Отказ с обоснованием
            Риск: 🟢 Низкий
            Рекомендация: ➡️ Передать юристу
```

#### Ежедневный дайджест (9:00 МСК)

В чат закупок автоматически:

```
⏱️ Претензии без ответа (>5 дней)
• №103831 | ООО Рыбный Мир | 5 076.65 BYN | 7 дн.
• №103845 | ИП Сидоров | 1 240.00 BYN | 6 дн.
```

---

### Мониторинг почты Exchange

После включения `EMAIL_ENABLED=true` система каждые N секунд:

1. Подключается к Exchange через EWS (NTLM)
2. Читает непрочитанные письма из `EMAIL_EWS_FOLDER`
3. Для каждого письма:
   - OCR вложений PDF/фото
   - Поиск номера претензии в теме и тексте
   - Создание записи в БД и постановка в очередь LLM
   - Пометка письма категорией `SRM_Processed`
   - Перемещение в папку `SRM_Обработано`
4. Уведомление в Telegram

**Типы обрабатываемых писем:**

| Тип | Признак | Как обрабатывается |
|-----|---------|-------------------|
| `email_formal` | Есть PDF/фото вложение | OCR вложения → приоритетный текст |
| `email_informal` | Только текст письма | Тело письма → передаётся в LLM |

**Письма без определённого номера претензии** не теряются — создаётся запись без привязки к претензии, закупки получают уведомление для ручной обработки.

```bash
# Проверить подключение к Exchange
curl http://localhost:8000/api/email/status
# {"enabled": true, "status": "ok", "unread_count": 3}

# Запустить опрос вручную (для отладки)
curl -X POST http://localhost:8000/api/email/poll
```

---

## Классификация ответов

| Код                    | Описание                                              | Маршрут       |
|------------------------|-------------------------------------------------------|---------------|
| `FULL_ACKNOWLEDGMENT`  | Полностью признаёт вину, готов оплатить               | → Юристу      |
| `PARTIAL_ACKNOWLEDGMENT`| Признаёт часть позиций или сумму                    | → Юристу      |
| `DENIAL`               | Отказ с конкретным обоснованием (договор, закон)     | → Юристу      |
| `DENIAL_NO_BASIS`      | Отказ без обоснования                                 | → Юристу      |
| `COUNTER_CLAIM`        | Встречное требование или зачёт                        | → Юристу ⚠️   |
| `DOCS_REQUEST`         | Запрашивает документы (ТТН, акты)                    | → Закупкам    |
| `DELAY_REQUEST`        | Просит отсрочку рассмотрения или оплаты              | → Закупкам    |
| `MANUAL_REVIEW`        | Противоречивый / неоднозначный ответ                 | → Закупкам    |
| `NO_RESPONSE`          | Ответ фактически отсутствует                          | → Закупкам    |

**Уровень риска:**

| Уровень    | Когда присваивается                                            |
|-----------|----------------------------------------------------------------|
| 🔴 `HIGH`  | Обоснованные возражения, ссылки на договор / закон            |
| 🟡 `MEDIUM`| Частично обоснован или неоднозначен                           |
| 🟢 `LOW`   | Позиция компании сильная, ответ поставщика слабый / отсутствует|

---

## Интеграция с ERP Маркет

### Адаптация маппинга

Откройте `backend/services/erp.py`, метод `_normalize_claim()`.  
Замените ключи под реальную JSON-структуру вашего ERP API:

```python
def _normalize_claim(self, raw: dict) -> dict:
    items = []
    for item in raw.get("КЛЮЧ_ПОЗИЦИЙ", []):       # ← заменить
        items.append({
            "sku":          item.get("АРТИКУЛ"),    # ← заменить
            "product_name": item.get("НАЗВАНИЕ"),   # ← заменить
            "ordered_qty":  float(item.get("ЗАКАЗАНО", 0)),  # ← заменить
            "received_qty": float(item.get("ПРИНЯТО", 0)),   # ← заменить
            # ... остальные поля
        })
    return {
        "claim_number": str(raw.get("НОМЕР_ПРЕТЕНЗИИ")),  # ← заменить
        # ...
    }
```

Ожидаемая структура на выходе из маппинга:

```json
{
  "claim_number": "103827",
  "order_number": "97486778",
  "supplier_name": "ООО Рыбный Мир",
  "supplier_inn": "123456789",
  "planned_delivery_date": "2026-05-06",
  "actual_delivery_date": "2026-05-07",
  "total_penalty_amount": 5076.65,
  "currency": "BYN",
  "items": [
    {
      "sku": "549906",
      "product_name": "Скумбрия БАРСКАЯ 300г",
      "ordered_qty": 5280.0,
      "received_qty": 2500.0,
      "promo_qty": 0.0,
      "allowed_deviation_pct": 0.0,
      "shortage_qty": 2780.0,
      "penalized_qty": 2780.0,
      "unit_price": 9.07,
      "penalty_rate_pct": 20.0,
      "penalty_amount": 5044.03
    }
  ]
}
```

### Тест без ERP

Если `ERP_API_URL` пустой — система использует встроенные mock-данные (претензия №103827). Позволяет проверить полный цикл до подключения реального API.

---

## Загрузка договоров

Договоры индексируются в ChromaDB и используются при LLM-анализе — система автоматически находит условия о штрафах, допустимых отклонениях и порядке рассмотрения претензий.

```bash
# 1. Создать поставщика
curl -X POST http://localhost:8000/api/suppliers \
  -F "name=ООО Рыбный Мир" \
  -F "inn=123456789"
# Ответ: {"id": 1, "name": "ООО Рыбный Мир"}

# 2. Загрузить основной договор
curl -X POST http://localhost:8000/api/contracts/upload \
  -F "file=@Договор_001_2024.pdf" \
  -F "supplier_id=1" \
  -F "contract_number=Д-2024/001" \
  -F "doc_type=main" \
  -F "valid_from=2024-01-01" \
  -F "valid_to=2026-12-31"

# 3. Загрузить дополнительное соглашение
curl -X POST http://localhost:8000/api/contracts/upload \
  -F "file=@ДС_001_2025.pdf" \
  -F "supplier_id=1" \
  -F "contract_number=ДС-2025/001" \
  -F "doc_type=additional_agreement"
```

**Рекомендации:**
- Загружайте все действующие договоры и доп. соглашения
- При обновлении договора загрузите новую версию с тем же `contract_number` — старые данные обновятся автоматически
- Формат только PDF. Word-документы — предварительно конвертировать

---

## Устранение неисправностей

### Exchange / Email

| Симптом | Причина | Решение |
|---------|---------|---------|
| `UnauthorizedError` в логах | Неверные учётные данные | Проверить `EMAIL_EWS_USER` (формат `DOMAIN\user`), регистр домена |
| `TransportError: SSL certificate` | Корп. сертификат | Установить `EMAIL_EWS_VERIFY_SSL=false` |
| `ErrorNonExistentMailbox` | Неверный `EMAIL_ADDRESS` | Взять SMTP-адрес из Active Directory |
| `ErrorAccessDenied` | Нет прав Delegate | Exchange Admin → Mailbox → Delegation → Full Access → `svc_srm` |
| Письма обрабатываются повторно | Нет прав на изменение категорий | Проверить права записи `svc_srm` на ящик |

```bash
# Проверка подключения
curl http://localhost:8000/api/email/status

# Логи email-обработки
docker compose logs celery_worker | grep -i exchange
```

### LLM / Ollama

| Симптом | Причина | Решение |
|---------|---------|---------|
| `processing_status: error` | Таймаут LLM | Увеличить `OLLAMA_TIMEOUT_SECONDS=300` |
| Плохое качество анализа | Модель 7b | Переключить на `qwen2.5:14b` (нужно 16 GB RAM) |
| Ollama не отвечает | Модель не загружена | `docker exec srm_ollama ollama pull qwen2.5:7b` |

```bash
# Список загруженных моделей
docker exec srm_ollama ollama list

# Ручной тест модели
docker exec -it srm_ollama ollama run qwen2.5:7b "Привет"

# Логи Ollama
docker compose logs ollama
```

### OCR

| Симптом | Причина | Решение |
|---------|---------|---------|
| Пустой `raw_text` | Плохое качество скана | Минимум 150 DPI, рекомендуется 300 DPI |
| Русский не распознаётся | Языковой пакет | `docker exec srm_backend tesseract --list-langs` — должны быть `eng, rus` |
| Номер претензии не определён | Нестандартный формат | Указать `claim_number` вручную при загрузке |

### PostgreSQL

```bash
# Подключиться к БД
docker exec -it srm_postgres psql -U claims_user -d claims_db

# Последние обработанные ответы
SELECT id, claim_id, classification, recommendation, processing_status, processed_at
FROM supplier_responses ORDER BY created_at DESC LIMIT 20;

# Претензии без ответа старше 7 дней
SELECT claim_number, total_penalty_amount, created_at
FROM claims WHERE status = 'pending'
  AND created_at < NOW() - INTERVAL '7 days';
```

### Celery

```bash
# Активные задачи
docker exec srm_celery celery -A workers.tasks inspect active

# Принудительный запуск синхронизации ERP
docker exec srm_celery celery -A workers.tasks call workers.tasks.sync_erp_claims

# Принудительный запуск опроса Exchange
docker exec srm_celery celery -A workers.tasks call workers.email_tasks.poll_and_process_emails
```

---

## Управление сервисами

```bash
# Статус
docker compose ps

# Логи в реальном времени
docker compose logs -f backend
docker compose logs -f celery_worker

# Перезапуск
docker compose restart backend
docker compose restart celery_worker

# Остановка (данные сохраняются)
docker compose down

# Полный сброс (УДАЛЯЕТ ВСЕ ДАННЫЕ)
docker compose down -v
```

### Обновление LLM-модели

```bash
docker exec srm_ollama ollama pull qwen2.5:14b
# В .env: OLLAMA_MODEL=qwen2.5:14b
docker compose restart celery_worker backend
```

### Резервное копирование

```bash
# База данных
docker exec srm_postgres pg_dump -U claims_user claims_db \
  | gzip > backup_$(date +%Y%m%d).sql.gz

# Загруженные файлы
tar -czf uploads_$(date +%Y%m%d).tar.gz uploads/

# ChromaDB (договоры)
docker compose stop chromadb
docker run --rm \
  -v supplier-response-management_chroma_data:/data \
  -v $(pwd):/backup \
  alpine tar -czf /backup/chroma_$(date +%Y%m%d).tar.gz /data
docker compose start chromadb
```

### Восстановление БД

```bash
gunzip -c backup_20260513.sql.gz \
  | docker exec -i srm_postgres psql -U claims_user claims_db
```

---

## Структура проекта

```
supplier-response-management/
├── backend/
│   ├── services/
│   │   ├── llm.py              # Промпты, Ollama-клиент, классификация
│   │   ├── ocr.py              # Tesseract OCR, извлечение текста
│   │   ├── erp.py              # ERP Маркет API клиент + маппинг
│   │   ├── rag.py              # ChromaDB — индексация и поиск договоров
│   │   └── email_exchange.py   # Exchange EWS клиент (NTLM)
│   ├── workers/
│   │   ├── tasks.py            # Celery: LLM-анализ, ERP-синхронизация
│   │   └── email_tasks.py      # Celery: периодический опрос Exchange
│   ├── main.py                 # FastAPI — все endpoint'ы
│   ├── models.py               # SQLAlchemy ORM
│   ├── database.py             # Подключение к PostgreSQL
│   ├── config.py               # Конфигурация (pydantic-settings)
│   ├── requirements.txt
│   └── Dockerfile
├── telegram_bot/
│   ├── bot.py                  # aiogram 3 бот
│   ├── requirements.txt
│   └── Dockerfile
├── init_db/
│   └── schema.sql              # DDL схемы PostgreSQL
├── scripts/
│   └── setup.sh                # Первичное развёртывание
├── uploads/                    # Файлы (в .gitignore)
├── docker-compose.yml
├── .env.example                # Шаблон конфигурации
├── .gitignore
├── LICENSE
└── README.md
```

---

## Безопасность

- LLM работает **полностью локально** (Ollama) — данные не передаются в облако
- Файлы хранятся **только на сервере** в `uploads/`
- `.env` с паролями **никогда не добавляется** в git
- Exchange-подключение через **сервисный аккаунт** с правами только на один ящик
- Telegram-бот ограничен **whitelist** (`TELEGRAM_ALLOWED_USERS`)

---

## Лицензия

MIT License — см. [LICENSE](LICENSE)
