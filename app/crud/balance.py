from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
from uuid import UUID
from app.models.user import UserBalance
from typing import Optional
import logging

api_logger = logging.getLogger("api")


async def async_update_or_create_balance(
    session: AsyncSession, 
    user_uuid: UUID, 
    ticker: str, 
    amount: int
) -> Optional[UserBalance]:
    
    try:
        result = await session.exec(
            select(UserBalance).where(
                UserBalance.user_uuid == user_uuid,
                UserBalance.ticker == ticker
            )
        )
        balance: Optional[UserBalance] = result.first()
        
        operation_type = "Updated"
        
        if balance:
            old_available = balance.available
            balance.available += amount
            session.add(balance) 
            
            api_logger.info(
                f'Balance updated for {ticker}',
                extra={
                    'user_uuid': str(user_uuid),
                    'ticker': ticker,
                    'amount_change': amount,
                    'available_before': old_available,
                    'available_after': balance.available
                }
            )
        else:
            operation_type = "Created"
            new_balance = UserBalance(
                user_uuid=user_uuid,
                ticker=ticker,
                available=amount,
                reserved=0
            )
            session.add(new_balance)
            balance = new_balance
            
            api_logger.info(
                f'New balance created for {ticker}',
                extra={
                    'user_uuid': str(user_uuid),
                    'ticker': ticker,
                    'initial_available': amount
                }
            )

        await session.flush()
        await session.refresh(balance)
        return balance

    except Exception as e:
        api_logger.error(
            f'Critical error during {operation_type} balance operation for user {user_uuid} and ticker {ticker}', 
            exc_info=e
        )
        raise