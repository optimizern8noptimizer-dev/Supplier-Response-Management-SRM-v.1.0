-- ============================================================
-- Supplier Response Management — Database Schema
-- ============================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Поставщики
CREATE TABLE suppliers (
    id          SERIAL PRIMARY KEY,
    inn         VARCHAR(20)  UNIQUE,
    name        VARCHAR(500) NOT NULL,
    created_at  TIMESTAMP    DEFAULT NOW()
);

-- Договоры поставщиков (загружаются из 1С-ДО вручную или через API)
CREATE TABLE contracts (
    id              SERIAL PRIMARY KEY,
    supplier_id     INTEGER REFERENCES suppliers(id) ON DELETE CASCADE,
    contract_number VARCHAR(200) NOT NULL,
    doc_type        VARCHAR(50)  DEFAULT 'main',    -- main | additional_agreement
    file_path       VARCHAR(500),                   -- путь к PDF на диске
    chroma_doc_id   VARCHAR(200),                   -- ID документа в ChromaDB
    valid_from      DATE,
    valid_to        DATE,
    uploaded_at     TIMESTAMP DEFAULT NOW(),
    UNIQUE(supplier_id, contract_number, doc_type)
);

-- Претензии (синхронизируются из ERP Маркет)
CREATE TABLE claims (
    id                    SERIAL PRIMARY KEY,
    claim_number          VARCHAR(50) UNIQUE NOT NULL,  -- Претензия №103827
    order_number          VARCHAR(50) NOT NULL,         -- Заказ №97486778
    supplier_id           INTEGER REFERENCES suppliers(id),
    planned_delivery_date DATE,
    actual_delivery_date  DATE,
    total_penalty_amount  DECIMAL(15,2),
    currency              VARCHAR(10)  DEFAULT 'BYN',
    status                VARCHAR(50)  DEFAULT 'pending',
        -- pending | response_received | processing | processed | transferred_to_legal | closed
    erp_raw               JSONB,       -- полный JSON из ERP API
    erp_synced_at         TIMESTAMP,
    created_at            TIMESTAMP DEFAULT NOW(),
    updated_at            TIMESTAMP DEFAULT NOW()
);

-- Позиции претензии
CREATE TABLE claim_items (
    id                   SERIAL PRIMARY KEY,
    claim_id             INTEGER REFERENCES claims(id) ON DELETE CASCADE,
    sku                  VARCHAR(50)  NOT NULL,
    product_name         VARCHAR(500),
    ordered_qty          DECIMAL(15,3),
    received_qty         DECIMAL(15,3),
    promo_qty            DECIMAL(15,3) DEFAULT 0,
    allowed_deviation_pct DECIMAL(5,2) DEFAULT 0,
    shortage_qty         DECIMAL(15,3),
    penalized_qty        DECIMAL(15,3),
    unit_price           DECIMAL(15,2),
    penalty_rate_pct     DECIMAL(5,2),
    penalty_amount       DECIMAL(15,2)
);

-- Ответы поставщиков
CREATE TABLE supplier_responses (
    id                  SERIAL PRIMARY KEY,
    claim_id            INTEGER REFERENCES claims(id) ON DELETE CASCADE,
    response_type       VARCHAR(50) DEFAULT 'manual_upload',
        -- manual_upload | email_formal | email_informal | no_response
    raw_text            TEXT,        -- извлечённый текст (OCR или email body)
    file_path           VARCHAR(500),
    file_original_name  VARCHAR(500),
    received_at         TIMESTAMP DEFAULT NOW(),
    submitted_by        VARCHAR(200), -- кто загрузил (пользователь или auto)

    -- Результаты LLM-анализа
    classification          VARCHAR(50),
        -- FULL_ACKNOWLEDGMENT | PARTIAL_ACKNOWLEDGMENT | DENIAL | DENIAL_NO_BASIS
        -- COUNTER_CLAIM | DOCS_REQUEST | DELAY_REQUEST | MANUAL_REVIEW | NO_RESPONSE
    supplier_position       TEXT,
    discrepancies           JSONB,   -- массив расхождений по позициям
    legal_basis_assessment  TEXT,
    legal_brief             TEXT,    -- готовая справка для юриста
    recommendation          VARCHAR(50),
        -- TRANSFER_TO_LEGAL | PROCUREMENT_REVIEW | REQUEST_CLARIFICATION | AWAIT_RESPONSE
    risk_level              VARCHAR(10),   -- HIGH | MEDIUM | LOW
    llm_notes               TEXT,

    -- Email метаданные (Exchange)
    email_message_id    VARCHAR(500) UNIQUE, -- Message-ID из Exchange для дедупликации
    email_sender        VARCHAR(300),
    email_subject       VARCHAR(500),

    -- Мета обработки
    llm_model           VARCHAR(100),
    processing_status   VARCHAR(50) DEFAULT 'pending',
        -- pending | processing | done | error
    processing_error    TEXT,
    processed_at        TIMESTAMP,
    created_at          TIMESTAMP DEFAULT NOW()
);

-- Лог действий (аудит)
CREATE TABLE audit_log (
    id          SERIAL PRIMARY KEY,
    entity_type VARCHAR(50),   -- claim | response | contract
    entity_id   INTEGER,
    action      VARCHAR(100),
    actor       VARCHAR(200),
    details     JSONB,
    created_at  TIMESTAMP DEFAULT NOW()
);

-- Индексы
CREATE INDEX idx_claims_number       ON claims(claim_number);
CREATE INDEX idx_claims_supplier     ON claims(supplier_id);
CREATE INDEX idx_claims_status       ON claims(status);
CREATE INDEX idx_claims_updated      ON claims(updated_at DESC);
CREATE INDEX idx_responses_claim     ON supplier_responses(claim_id);
CREATE INDEX idx_responses_class     ON supplier_responses(classification);
CREATE INDEX idx_responses_status    ON supplier_responses(processing_status);
CREATE INDEX idx_response_recom      ON supplier_responses(recommendation);
CREATE INDEX idx_items_claim         ON claim_items(claim_id);
CREATE INDEX idx_items_sku           ON claim_items(sku);
CREATE INDEX idx_contracts_supplier  ON contracts(supplier_id);

CREATE INDEX idx_responses_email_msgid ON supplier_responses(email_message_id);

-- Триггер обновления updated_at для claims
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_claims_updated
    BEFORE UPDATE ON claims
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
