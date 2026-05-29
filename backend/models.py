from sqlalchemy import (
    Column, Integer, String, Text, DateTime, Date,
    Numeric, ForeignKey, JSON, func
)
from sqlalchemy.orm import relationship
from database import Base


class Supplier(Base):
    __tablename__ = "suppliers"

    id         = Column(Integer, primary_key=True)
    inn        = Column(String(20), unique=True)
    name       = Column(String(500), nullable=False)
    created_at = Column(DateTime, default=func.now())

    claims    = relationship("Claim", back_populates="supplier")
    contracts = relationship("Contract", back_populates="supplier")


class Contract(Base):
    __tablename__ = "contracts"

    id              = Column(Integer, primary_key=True)
    supplier_id     = Column(Integer, ForeignKey("suppliers.id", ondelete="CASCADE"))
    contract_number = Column(String(200), nullable=False)
    doc_type        = Column(String(50), default="main")
    file_path       = Column(String(500))
    chroma_doc_id   = Column(String(200))
    valid_from      = Column(Date)
    valid_to        = Column(Date)
    uploaded_at     = Column(DateTime, default=func.now())

    supplier = relationship("Supplier", back_populates="contracts")


class Claim(Base):
    __tablename__ = "claims"

    id                    = Column(Integer, primary_key=True)
    claim_number          = Column(String(50), unique=True, nullable=False)
    order_number          = Column(String(50), nullable=False)
    supplier_id           = Column(Integer, ForeignKey("suppliers.id"))
    planned_delivery_date = Column(Date)
    actual_delivery_date  = Column(Date)
    total_penalty_amount  = Column(Numeric(15, 2))
    currency              = Column(String(10), default="BYN")
    status                = Column(String(50), default="pending")
    erp_raw               = Column(JSON)
    erp_synced_at         = Column(DateTime)
    created_at            = Column(DateTime, default=func.now())
    updated_at            = Column(DateTime, default=func.now(), onupdate=func.now())

    supplier  = relationship("Supplier", back_populates="claims")
    items     = relationship("ClaimItem", back_populates="claim", cascade="all, delete-orphan")
    responses = relationship("SupplierResponse", back_populates="claim", cascade="all, delete-orphan")


class ClaimItem(Base):
    __tablename__ = "claim_items"

    id                    = Column(Integer, primary_key=True)
    claim_id              = Column(Integer, ForeignKey("claims.id", ondelete="CASCADE"))
    sku                   = Column(String(50), nullable=False)
    product_name          = Column(String(500))
    ordered_qty           = Column(Numeric(15, 3))
    received_qty          = Column(Numeric(15, 3))
    promo_qty             = Column(Numeric(15, 3), default=0)
    allowed_deviation_pct = Column(Numeric(5, 2), default=0)
    shortage_qty          = Column(Numeric(15, 3))
    penalized_qty         = Column(Numeric(15, 3))
    unit_price            = Column(Numeric(15, 2))
    penalty_rate_pct      = Column(Numeric(5, 2))
    penalty_amount        = Column(Numeric(15, 2))

    claim = relationship("Claim", back_populates="items")


class SupplierResponse(Base):
    __tablename__ = "supplier_responses"

    id                    = Column(Integer, primary_key=True)
    claim_id              = Column(Integer, ForeignKey("claims.id", ondelete="CASCADE"))
    response_type         = Column(String(50), default="manual_upload")
    raw_text              = Column(Text)
    file_path             = Column(String(500))
    file_original_name    = Column(String(500))
    received_at           = Column(DateTime, default=func.now())
    submitted_by          = Column(String(200))

    # LLM results
    classification         = Column(String(50))
    supplier_position      = Column(Text)
    discrepancies          = Column(JSON)
    legal_basis_assessment = Column(Text)
    legal_brief            = Column(Text)
    recommendation         = Column(String(50))
    risk_level             = Column(String(10))
    llm_notes              = Column(Text)

    # Email metadata (Exchange)
    email_message_id   = Column(String(500), unique=True)  # Message-ID из Exchange — для дедупликации
    email_sender       = Column(String(300))
    email_subject      = Column(String(500))

    # Meta
    llm_model          = Column(String(100))
    processing_status  = Column(String(50), default="pending")
    processing_error   = Column(Text)
    processed_at       = Column(DateTime)
    created_at         = Column(DateTime, default=func.now())

    claim = relationship("Claim", back_populates="responses")


class AuditLog(Base):
    __tablename__ = "audit_log"

    id          = Column(Integer, primary_key=True)
    entity_type = Column(String(50))
    entity_id   = Column(Integer)
    action      = Column(String(100))
    actor       = Column(String(200))
    details     = Column(JSON)
    created_at  = Column(DateTime, default=func.now())
