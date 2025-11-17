from sqlalchemy import Column, ForeignKey
from sqlmodel import Field, SQLModel, Relationship
from uuid import UUID, uuid4
from enum import Enum as PyEnum
from datetime import datetime, timezone
from typing import Optional, List

from app.schemas.openapi_schemas import Direction, OrderStatus 

class Side(str, PyEnum):
    BUY = "BUY"
    SELL = "SELL"

class Order(SQLModel, table=True):
    id: UUID = Field(default_factory=uuid4, primary_key=True, index=True)
    user_uuid: UUID = Field(
        sa_column=Column(ForeignKey("user.uuid", ondelete="CASCADE"), index=True)
    )
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    
    side: Side 
    ticker: str = Field(
        sa_column=Column(ForeignKey("instrument.ticker", ondelete="CASCADE"))
    )
    qty: int
    price: Optional[int] = None
    
    status: OrderStatus = OrderStatus.NEW
    filled: int = 0
    
    user: "User" = Relationship(back_populates="orders")
    trades: List["Trade"] = Relationship(
        back_populates="order", 
        sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )
class Trade(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    order_id: UUID = Field(
        sa_column=Column(ForeignKey("order.id", ondelete="CASCADE")) 
    )
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    ticker: str = Field(
        sa_column=Column(ForeignKey("instrument.ticker", ondelete="CASCADE"))
    )
    quantity: int
    price: int
    
    order: Order = Relationship(back_populates="trades")