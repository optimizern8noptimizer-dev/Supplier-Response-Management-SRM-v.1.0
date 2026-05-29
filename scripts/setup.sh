#!/bin/bash
# =============================================================
# Supplier Response Management — Первичная установка
# Запускать от root или пользователя с sudo на Ubuntu/Debian
# =============================================================

set -e
echo "=== SRM Setup Script ==="

# 1. Проверяем Docker
if ! command -v docker &> /dev/null; then
    echo "[ERROR] Docker не установлен. Установите Docker и docker compose plugin."
    exit 1
fi

# 2. Создаём .env из шаблона
if [ ! -f .env ]; then
    cp .env.example .env
    echo "[OK] Создан .env из шаблона. ОБЯЗАТЕЛЬНО отредактируйте его перед запуском!"
    echo "     nano .env"
    exit 0
fi

# 3. Создаём директории
mkdir -p uploads/responses uploads/contracts uploads/temp

# 4. Запускаем инфраструктуру
echo "[1/4] Запуск PostgreSQL, Redis, ChromaDB..."
docker compose up -d postgres redis chromadb
sleep 10

# 5. Запуск Ollama и загрузка моделей
echo "[2/4] Запуск Ollama..."
docker compose up -d ollama
sleep 5

echo "[3/4] Загрузка LLM-модели (может занять 10-30 минут при первом запуске)..."
# Определяем модель из .env
OLLAMA_MODEL=$(grep OLLAMA_MODEL .env | cut -d= -f2 | tr -d '"' | tr -d ' ')
OLLAMA_EMBED=$(grep OLLAMA_EMBED_MODEL .env | cut -d= -f2 | tr -d '"' | tr -d ' ')

echo "  Модель анализа: ${OLLAMA_MODEL:-qwen2.5:7b}"
echo "  Модель эмбеддингов: ${OLLAMA_EMBED:-nomic-embed-text}"

docker exec srm_ollama ollama pull "${OLLAMA_MODEL:-qwen2.5:7b}"
docker exec srm_ollama ollama pull "${OLLAMA_EMBED:-nomic-embed-text}"

# 6. Запускаем приложение
echo "[4/4] Запуск backend, Celery, Telegram бота..."
docker compose up -d backend celery_worker celery_beat telegram_bot

echo ""
echo "=== УСТАНОВКА ЗАВЕРШЕНА ==="
echo ""
echo "  Web UI:  http://localhost:8000"
echo "  API:     http://localhost:8000/docs"
echo "  Ollama:  http://localhost:11434"
echo ""
echo "Проверка статуса: docker compose ps"
echo "Логи backend:     docker compose logs -f backend"
echo "Логи Celery:      docker compose logs -f celery_worker"
