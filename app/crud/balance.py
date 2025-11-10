from sqlmodel import Session, select
from uuid import UUID
from app.models.user import UserBalance

def update_or_create_balance(session: Session, user_uuid: UUID, ticker: str, amount: int):
    balance = session.exec(
        select(UserBalance).where(
            UserBalance.user_uuid == user_uuid,
            UserBalance.ticker == ticker
        )
    ).first()

    if balance:
        balance.available += amount
        session.add(balance)
    else:
        new_balance = UserBalance(
            user_uuid=user_uuid,
            ticker=ticker,
            available=amount,
            reserved=0
        )
        session.add(new_balance)