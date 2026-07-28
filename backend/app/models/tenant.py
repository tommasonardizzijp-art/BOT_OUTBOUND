import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum as SAEnum, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class TenantStatus(str, enum.Enum):
    active = "active"
    suspended = "suspended"


class Tenant(Base):
    """Il cliente della piattaforma. Non e' solo WhatsApp: esiste da qui in poi
    anche per il canale IG, quando si unifichera' la UI (SDD 5.1)."""
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True,
                                    default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[TenantStatus] = mapped_column(
        SAEnum(TenantStatus, name="tenant_status", native_enum=False),
        default=TenantStatus.active, nullable=False)
    settings: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=datetime.utcnow, nullable=False)
