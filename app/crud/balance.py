from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
from uuid import UUID
from app.models.user import UserBalance
from typing import Optional, Union
import logging
from sqlalchemy import select as sa_select

api_logger = logging.getLogger("api")

class BalanceError(Exception):
    pass

async def _check_and_delete_balance(session: AsyncSession, balance: UserBalance):
    if balance.available == 0 and balance.reserved == 0:
        ticker = balance.ticker
        user_uuid_str = str(balance.user_uuid)
        
        await session.delete(balance)
        
        api_logger.info(
            f'Zero balance entry deleted. User: {user_uuid_str}, Ticker: {ticker}'
        )

async def async_update_or_create_balance(
    session: AsyncSession, 
    user_uuid: UUID, 
    ticker: str, 
    amount: int
) -> Optional[UserBalance]:
    user_uuid_str = str(user_uuid)
    operation_type = "Update"
    
    try:
        result = await session.exec(
            select(UserBalance).where(
                UserBalance.user_uuid == user_uuid,
                UserBalance.ticker == ticker
            ).with_for_update() 
        )
        balance: Optional[UserBalance] = result.first()
        
        if balance:
            old_available = balance.available
            
            balance.available += amount
            session.add(balance) 
            
            api_logger.info(
                f'Balance updated for {ticker}',
                extra={
                    'user_uuid': user_uuid_str,
                    'ticker': ticker,
                    'amount_change': amount,
                    'available_before': old_available,
                    'available_after': balance.available
                }
            )
            
            await _check_and_delete_balance(session, balance) 

        else:
            operation_type = "Create"
            if amount > 0:
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
                        'user_uuid': user_uuid_str,
                        'ticker': ticker,
                        'initial_available': amount
                    }
                )
            else:
                return None 

        await session.flush()
        
        is_balance_persistent = balance and (balance.available != 0 or balance.reserved != 0)
        
        if is_balance_persistent:
            await session.refresh(balance)
            return balance
        
        return None

    except Exception as e:
        api_logger.error(
            f'Critical error during {operation_type} balance operation for user {user_uuid_str} and ticker {ticker}', 
            exc_info=e
        )
        raise