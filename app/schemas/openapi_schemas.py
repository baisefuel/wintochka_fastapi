from pydantic import BaseModel, Field, constr, UUID4
from uuid import UUID
from typing import List, Dict, Optional, Union, Literal
from datetime import datetime
from enum import Enum

class Direction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

class OrderStatus(str, Enum):
    NEW = "NEW"
    EXECUTED = "EXECUTED"
    PARTIALLY_EXECUTED = "PARTIALLY_EXECUTED"
    CANCELLED = "CANCELLED"

class UserRole(str, Enum):
    USER = "USER"
    ADMIN = "ADMIN"

class NewUser(BaseModel):
    name: str = Field(..., min_length=3)

class User(BaseModel):
    id: UUID4
    name: str
    role: UserRole
    api_key: str

class Instrument(BaseModel):
    name: str
    ticker: constr(pattern=r"^[A-Z]{2,10}$")

class Level(BaseModel):
    price: int
    qty: int

class L2OrderBook(BaseModel):
    bid_levels: List[Level]
    ask_levels: List[Level]

class Transaction(BaseModel):
    ticker: str
    amount: int
    price: int
    timestamp: datetime

class LimitOrderBody(BaseModel):
    direction: Direction
    ticker: str
    qty: int = Field(..., ge=1)
    price: int = Field(..., gt=0)

class MarketOrderBody(BaseModel):
    direction: Direction
    ticker: str
    qty: int = Field(..., ge=1)

class LimitOrder(BaseModel):
    id: UUID4
    status: OrderStatus
    user_id: UUID4 
    timestamp: datetime
    body: LimitOrderBody
    filled: int = 0
    class Config:
        title = "LimitOrder"

class MarketOrder(BaseModel):
    id: UUID4
    status: OrderStatus
    user_id: UUID4
    timestamp: datetime
    body: MarketOrderBody
    class Config:
        title = "MarketOrder"

class CreateOrderResponse(BaseModel):
    success: Literal[True] = True
    order_id: UUID4

class Ok(BaseModel):
    success: Literal[True] = True

class Body_deposit_api_v1_admin_balance_deposit_post(BaseModel):
    user_id: UUID
    ticker: str
    amount: int = Field(..., gt=0)

class Body_withdraw_api_v1_admin_balance_withdraw_post(BaseModel):
    user_id: UUID
    ticker: str
    amount: int = Field(..., gt=0)

class ValidationError(BaseModel):
    loc: List[Union[str, int]]
    msg: str
    type: str

class HTTPValidationError(BaseModel):
    detail: Optional[List[ValidationError]] = None