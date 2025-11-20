from sqlalchemy import Column, ForeignKey, UniqueConstraint
from sqlmodel import Field, SQLModel, Relationship
from uuid import UUID, uuid4
from enum import Enum as PyEnum
from typing import List, Optional

class UserRole(str, PyEnum):
    USER = "USER"
    ADMIN = "ADMIN"

class User(SQLModel, table=True):
    uuid: UUID = Field(default_factory=uuid4, primary_key=True, index=True)
    name: str
    role: UserRole
    api_key: str = Field(index=True, unique=True)
    is_active: bool = True
    balances: List["UserBalance"] = Relationship(
            back_populates="user",
            sa_relationship_kwargs={"cascade": "all, delete-orphan"}
        )
    orders: List["Order"] = Relationship(
            back_populates="user",
            sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )
class UserBalance(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_uuid: UUID = Field(
        sa_column=Column(ForeignKey("user.uuid", ondelete="CASCADE"), index=True)
    )
    ticker: str = Field(
        sa_column=Column(ForeignKey("instrument.ticker", ondelete="CASCADE"), index=True)
    )
    available: int = 0
    reserved: int = 0
    
    user: "User" = Relationship(back_populates="balances")

    __table_args__ = (
        UniqueConstraint(
            "user_uuid", "ticker", name="unique_user_ticker_constraint"
        ),
    )