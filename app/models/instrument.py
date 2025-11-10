from sqlmodel import Field, SQLModel
from typing import Optional

class Instrument(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    ticker: str = Field(unique=True, index=True)
    is_active: bool = True