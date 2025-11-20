from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
from uuid import UUID
from typing import Optional, Union
import logging
from app.core.config import settings
from sqlalchemy import select as sa_select
from sqlalchemy.dialects.postgresql import insert

from app.models.user import UserBalance


api_logger = logging.getLogger("api")

class BalanceError(Exception):
    pass

async def async_update_or_create_balance(
    session: AsyncSession, 
    user_uuid: UUID, 
    ticker: str, 
    amount: int
) -> Optional[UserBalance]:

    if amount == 0:
        return None
        
    user_uuid_str = str(user_uuid)
    
    insert_stmt = insert(UserBalance).values(
        user_uuid=user_uuid,
        ticker=ticker,
        available=amount,
        reserved=0
    )

    do_update_stmt = insert_stmt.on_conflict_do_update(
        index_elements=['user_uuid', 'ticker'],
        set_={
            'available': UserBalance.available + amount
        }
    ).returning(UserBalance)
    
    operation_type = "Upsert"
    
    try:
        result = await session.execute(do_update_stmt)
        updated_balance: Optional[UserBalance] = result.scalars().first()
        
        if updated_balance:
            
            api_logger.info(
                f'Balance {operation_type}d for {ticker}',
                extra={
                    'user_uuid': user_uuid_str,
                    'ticker': ticker,
                    'amount_change': amount,
                    'available_after': updated_balance.available
                }
            )
            
            if updated_balance.available < 0:
                error_msg = f"Available balance became negative after update for {ticker}. Value: {updated_balance.available}"
                api_logger.critical(error_msg)
                raise BalanceError(error_msg)
                
            return updated_balance
        
        return None

    except Exception as e:
        api_logger.error(
            f'Critical error during {operation_type} balance operation for user {user_uuid_str} and ticker {ticker}', 
            exc_info=e
        )
        raise


async def async_debit_available_balance(
    session: AsyncSession, user_uuid: UUID, ticker: str, amount: int
) -> Optional[UserBalance]:
    if amount <= 0:
        return None

    
    updated_balance = await async_update_or_create_balance(
        session, user_uuid, ticker, -amount
    )

    if updated_balance and updated_balance.available < 0:
        error_msg = f"Attempted debit failed. Insufficient available {ticker}. Needed {amount}, result available: {updated_balance.available}"
        api_logger.critical(error_msg)
        raise BalanceError(error_msg)

    return updated_balance