from sqlmodel import SQLModel, Field
from enum import Enum
from datetime import datetime
from typing import Optional

class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

class Order(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    ticker: str
    side: Side
    price: float
    quantity: float
    filled: float = 0.0
    user_id: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)

    @property
    def remaining(self) -> float:
        return max(0.0, self.quantity - self.filled)
