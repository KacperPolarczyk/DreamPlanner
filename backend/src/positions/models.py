"""Position ORM model from the reference PostgreSQL schema."""

from sqlalchemy import BigInteger, Boolean, CheckConstraint, Identity, String, text
from sqlalchemy.orm import Mapped, mapped_column

from src.core.database import Base


class Position(Base):
    """A position that may be required on a shift."""

    __tablename__ = "positions"
    __table_args__ = (
        CheckConstraint("length(btrim(name)) > 0", name="positions_name_not_blank"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
