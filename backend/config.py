from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    # Database
    DATABASE_URL: str = "postgresql://claims_user:password@localhost:5432/claims_db"

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"

    # Ollama
    OLLAMA_URL: str = "http://localhost:11434"
    OLLAMA_MODEL: str = "qwen2.5:7b"
    OLLAMA_EMBED_MODEL: str = "nomic-embed-text"
    OLLAMA_TIMEOUT_SECONDS: int = 180  # LLM медленный на CPU

    # ChromaDB
    CHROMA_HOST: str = "localhost"
    CHROMA_PORT: int = 8000

    # ERP Маркет API
    ERP_API_URL: str = ""
    ERP_API_TOKEN: str = ""
    ERP_SYNC_INTERVAL_MINUTES: int = 30

    # Telegram
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_ALLOWED_USERS: str = ""          # "123,456,789"
    TELEGRAM_LEGAL_CHAT_ID: Optional[str] = None
    TELEGRAM_PROCUREMENT_CHAT_ID: Optional[str] = None

    # ── Exchange / EWS ────────────────────────────────────────────────────────
    EMAIL_ENABLED: bool = False

    # Адрес ящика для мониторинга (SMTP-адрес почтового ящика в Exchange)
    EMAIL_ADDRESS: str = ""                      # claims@company.by

    # EWS endpoint — обычно https://mail.company.by/EWS/Exchange.asmx
    # Если autodiscover не работает — укажите явно
    EMAIL_EWS_HOST: str = ""                     # mail.company.by (только хост, без /EWS/...)

    # Учётная запись для подключения (может отличаться от EMAIL_ADDRESS)
    # Формат NTLM: DOMAIN\username  |  Формат Basic: user@domain.local
    EMAIL_EWS_USER: str = ""                     # COMPANY\svc_claims или svc_claims@company.local
    EMAIL_PASSWORD: str = ""

    # Метод аутентификации: ntlm (рекомендуется для on-prem) | basic
    EMAIL_AUTH_TYPE: str = "ntlm"

    # Проверять SSL-сертификат Exchange. False — для корп. CA / самоподписанных
    EMAIL_EWS_VERIFY_SSL: bool = False

    # Папка для мониторинга (INBOX или имя подпапки)
    EMAIL_EWS_FOLDER: str = "INBOX"

    # Интервал опроса в секундах
    EMAIL_CHECK_INTERVAL_SECONDS: int = 60

    # Максимум писем за один опрос (защита от лавины при первом запуске)
    EMAIL_BATCH_SIZE: int = 20

    # Security
    API_SECRET_KEY: str = "dev-secret-key-change-in-production"
    API_TOKEN: str = ""

    # Uploads
    UPLOAD_MAX_SIZE_MB: int = 50
    UPLOAD_DIR: str = "/app/uploads"

    LOG_LEVEL: str = "INFO"

    class Config:
        env_file = ".env"
        extra = "ignore"

    @property
    def allowed_telegram_users(self) -> list[int]:
        if not self.TELEGRAM_ALLOWED_USERS:
            return []
        return [int(uid.strip()) for uid in self.TELEGRAM_ALLOWED_USERS.split(",") if uid.strip()]


settings = Settings()
