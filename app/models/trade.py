from sqlmodel import SQLModel, Field
from datetime import datetime
from typing import Optional

class Trade(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    ticker: str
    price: float
    quantity: float
    buy_order_id: int
    sell_order_id: int
    timestamp: datetime = Field(default_factory=datetime.utcnow)
