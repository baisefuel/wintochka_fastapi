from sqlmodel import Field, SQLModel, Relationship
from uuid import UUID, uuid4
from enum import Enum as PyEnum
from datetime import datetime
from typing import Optional, List

from app.schemas.openapi_schemas import Direction, OrderStatus 

class Side(str, PyEnum):
    BUY = "BUY"
    SELL = "SELL"

class Order(SQLModel, table=True):
    id: UUID = Field(default_factory=uuid4, primary_key=True, index=True)
    user_uuid: UUID = Field(foreign_key="user.uuid", index=True)
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    
    side: Side 
    ticker: str
    qty: int
    price: Optional[int] = None
    
    status: OrderStatus = OrderStatus.NEW
    filled: int = 0
    
    user: "User" = Relationship(back_populates="orders")
    trades: List["Trade"] = Relationship(back_populates="order")

class Trade(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    order_id: UUID = Field(foreign_key="order.id", index=True)
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    ticker: str
    quantity: int
    price: int
    
    order: Order = Relationship(back_populates="trades")