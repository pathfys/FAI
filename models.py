from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from utils.helpers import now_utc


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    api_id: Mapped[str] = mapped_column(String(256), nullable=False)
    api_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    phone: Mapped[str] = mapped_column(String(32), nullable=False)
    session_string: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


class CampaignAccount(Base):
    __tablename__ = "campaign_accounts"

    campaign_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("campaigns.id", ondelete="CASCADE"), primary_key=True
    )
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    )


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    interval_seconds: Mapped[int] = mapped_column(Integer, default=30)
    status: Mapped[str] = mapped_column(String(16), default="draft")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    accounts: Mapped[list[Account]] = relationship(
        "Account", secondary="campaign_accounts", lazy="selectin"
    )
    recipients: Mapped[list["Recipient"]] = relationship(
        "Recipient", back_populates="campaign", lazy="selectin"
    )


class Recipient(Base):
    __tablename__ = "recipients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    target: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    campaign: Mapped[Campaign] = relationship("Campaign", back_populates="recipients")


class SentLog(Base):
    __tablename__ = "sent_log"
    __table_args__ = (
        Index("ix_sent_log_dedup", "account_id", "target", "campaign_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    recipient_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("recipients.id", ondelete="CASCADE"), nullable=False
    )
    campaign_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    target: Mapped[str] = mapped_column(String(256), nullable=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
