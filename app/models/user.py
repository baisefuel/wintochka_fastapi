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
    
    balances: List["UserBalance"] = Relationship(back_populates="user")
    orders: List["Order"] = Relationship(back_populates="user")

class UserBalance(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_uuid: UUID = Field(foreign_key="user.uuid", index=True)
    ticker: str = Field(index=True)
    available: int = 0
    reserved: int = 0
    
    user: "User" = Relationship(back_populates="balances")