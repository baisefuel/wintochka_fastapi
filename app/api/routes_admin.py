from fastapi import APIRouter, Depends, HTTPException, Path, status
from sqlalchemy import select
from sqlmodel.ext.asyncio.session import AsyncSession
from uuid import UUID
import logging
from typing import Union

from app.api.match_engine import async_cancel_order_and_unreserve
from app.core.db import get_async_session
from app.api.deps import get_current_admin
from app.models.user import User as UserModel, UserBalance
from app.models.instrument import Instrument as InstrumentModel
from app.models.order import Order as OrderModel
from app.schemas.openapi_schemas import (
    Ok, OrderStatus, User, Instrument as InstrumentSchema, 
    Body_deposit_api_v1_admin_balance_deposit_post as DepositBody,
    Body_withdraw_api_v1_admin_balance_withdraw_post as WithdrawBody
)
from app.crud.balance import async_update_or_create_balance

api_logger = logging.getLogger("api")

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

@router.delete("/user/{user_id}", 
               response_model=User, 
               summary="Delete User",
               tags=["user"],
               description="Фактическое удаление пользователя (Hard Delete) и всех связанных данных.")
async def delete_user(
    user_id: UUID = Path(..., title="User Id"),
    admin_user: UserModel = Depends(get_current_admin),
    session: AsyncSession = Depends(get_async_session)
):
    admin_uuid_str = str(admin_user.uuid)
    try:
        user: Union[UserModel, None] = (await session.exec(select(UserModel).where(UserModel.uuid == user_id))).scalars().first()
        
        if not user or not user.is_active:
            status_detail = "not found" if not user else "already inactive"
            api_logger.warning(f'Admin {admin_uuid_str} failed to delete user: {user_id}. User {status_detail}.')
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        
        active_orders_stmt = select(OrderModel).where(
            OrderModel.user_uuid == user_id, 
            OrderModel.status.in_([OrderStatus.NEW, OrderStatus.PARTIALLY_EXECUTED])
        ).with_for_update() 
        
        active_orders = (await session.exec(active_orders_stmt)).scalars().all()
        
        for order in active_orders:
            await async_cancel_order_and_unreserve(session, order)
            
        balances_stmt = select(UserBalance).where(UserBalance.user_uuid == user_id)
        user_balances = (await session.exec(balances_stmt.with_for_update())).scalars().all()
        
        for balance in user_balances:
            if balance.reserved != 0:
                api_logger.critical(
                    f'User {user_id} balance check failed: reserved amount is {balance.reserved} after order cancellation. Correcting reserved funds before delete.'
                )
                balance.available += balance.reserved
                balance.reserved = 0
            

        await session.delete(user)
        await session.commit()
        
        api_logger.info(
            f'User permanently DELETED (cascaded) by admin {admin_uuid_str}. Target User ID: {user_id}'
        )
        
        return User(id=user.uuid, name=user.name, role=user.role, api_key="DELETED")
        
    except HTTPException:
        raise
    except Exception as e:
        await session.rollback()
        api_logger.error(f'Critical error during user hard delete for {user_id} by admin {admin_uuid_str}', exc_info=e)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error during user deletion.")


@router.post("/balance/deposit", 
             response_model=Ok, 
             summary="Deposit",
             tags=["balance"],
             description="Пополнение баланса пользователя.")
async def deposit(
    body: DepositBody,
    admin_user: UserModel = Depends(get_current_admin), 
    session: AsyncSession = Depends(get_async_session)
):
    admin_uuid_str = str(admin_user.uuid)
    
    try:
        user = (await session.exec(select(UserModel).where(UserModel.uuid == body.user_id))).scalars().first()
        
        if not user or not user.is_active:
            status_detail = "not found" if not user else "inactive"
            api_logger.warning(f'Admin {admin_uuid_str} failed deposit to {body.user_id}: User {status_detail}.')
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        
        instrument = (
            await session.exec(select(InstrumentModel).where(InstrumentModel.ticker == body.ticker))
        ).scalars().first()
        
        if not instrument:
            api_logger.warning(
                f'Admin {admin_uuid_str} failed deposit to {body.user_id}: Ticker {body.ticker} not found.'
            )
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Instrument not found.")
            
        await async_update_or_create_balance(session, body.user_id, body.ticker, body.amount)
        await session.commit()
        
        api_logger.info(
            f'Deposit successful by admin {admin_uuid_str}. Target User ID: {body.user_id}, Ticker: {body.ticker}, Amount: {body.amount}'
        )
        
        return Ok()
        
    except HTTPException:
        raise
    except Exception as e:
        await session.rollback()
        api_logger.error(
            f'Critical error during deposit for {body.user_id} of {body.amount} {body.ticker} by admin {admin_uuid_str}', 
            exc_info=e
        )
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error during balance deposit.")

@router.post("/balance/withdraw", 
             response_model=Ok, 
             summary="Withdraw",
             tags=["balance"],
             description="Списание доступных средств с баланса пользователя.")
async def withdraw(
    body: WithdrawBody,
    admin_user: UserModel = Depends(get_current_admin), 
    session: AsyncSession = Depends(get_async_session)
):
    admin_uuid_str = str(admin_user.uuid)
    
    try:
        user = (await session.exec(select(UserModel).where(UserModel.uuid == body.user_id))).scalars().first()
        
        if not user or not user.is_active:
            status_detail = "not found" if not user else "inactive"
            api_logger.warning(f'Admin {admin_uuid_str} failed withdraw from {body.user_id}: User {status_detail}.')
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
            
        balance: Union[UserBalance, None] = (await session.exec(
            select(UserBalance).where(
                UserBalance.user_uuid == body.user_id, 
                UserBalance.ticker == body.ticker
            ).with_for_update()
        )).scalars().first()
        
        available_qty = balance.available if balance else 0
        
        if not balance or balance.available < body.amount:
            api_logger.warning(
                f'Admin {admin_uuid_str} failed withdraw from {body.user_id}: Insufficient balance. Ticker: {body.ticker}, Requested: {body.amount}, Available: {available_qty}'
            )
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, 
                detail="Insufficient available balance or ticker not found."
            )
            
        await async_update_or_create_balance(session, body.user_id, body.ticker, -body.amount)
        await session.commit()
        
        api_logger.info(
            f'Withdrawal successful by admin {admin_uuid_str}. Target User ID: {body.user_id}, Ticker: {body.ticker}, Amount: {body.amount}'
        )
        
        return Ok()
    except HTTPException:
        raise
    except Exception as e:
        await session.rollback()
        api_logger.error(
            f'Critical error during withdraw for {body.user_id} of {body.amount} {body.ticker} by admin {admin_uuid_str}', 
            exc_info=e
        )
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error during balance withdrawal.")


@router.post("/instrument", 
              response_model=Ok, 
              summary="Add Instrument",
              description="Добавление нового торгового инструмента (или реактивация).")
async def add_instrument(
    body: InstrumentSchema,
    admin_user: UserModel = Depends(get_current_admin), 
    session: AsyncSession = Depends(get_async_session)
):
    admin_uuid_str = str(admin_user.uuid)
    
    try:
        existing: Union[InstrumentModel, None] = (await session.exec(select(InstrumentModel).where(InstrumentModel.ticker == body.ticker))).scalars().first()
        
        if existing:
            if existing.is_active:
                api_logger.warning(f'Admin {admin_uuid_str} failed to add instrument: {body.ticker} already exists and is active.')
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Instrument with this ticker already exists.")
            else:
                existing.name = body.name
                existing.is_active = True
                session.add(existing)
                await session.commit()
                
                api_logger.info(
                    f'Instrument successfully REACTIVATED by admin {admin_uuid_str}. Ticker: {body.ticker}, Name: {body.name}'
                )
                return Ok()
                
        instrument = InstrumentModel(name=body.name, ticker=body.ticker, is_active=True)
        session.add(instrument)
        await session.commit()
        
        api_logger.info(
            f'Instrument successfully ADDED by admin {admin_uuid_str}. Ticker: {body.ticker}, Name: {body.name}'
        )
        
        return Ok()
    except HTTPException:
        raise
    except Exception as e:
        await session.rollback()
        api_logger.error(f'Critical error adding instrument {body.ticker} by admin {admin_uuid_str}', exc_info=e)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error adding instrument.")


@router.delete("/instrument/{ticker}", 
               response_model=Ok, 
               summary="Delete Instrument",
               description="Фактическое удаление (Hard Delete) инструмента. Отменяет все активные ордера по этому тикеру.")
async def delete_instrument(
    ticker: str = Path(..., title="Ticker"),
    admin_user: UserModel = Depends(get_current_admin), 
    session: AsyncSession = Depends(get_async_session)
):
    admin_uuid_str = str(admin_user.uuid)
    
    try:
        instrument: Union[InstrumentModel, None] = (await session.exec(select(InstrumentModel).where(InstrumentModel.ticker == ticker))).scalars().first()
        
        if not instrument:
            api_logger.warning(f'Admin {admin_uuid_str} failed to delete instrument: {ticker} not found.')
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Instrument not found.")
        
        if not instrument.is_active:
            api_logger.warning(f'Admin {admin_uuid_str} attempted to delete instrument {ticker} which is already inactive.')
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Instrument not found.")

        active_orders_stmt = select(OrderModel).where(
            OrderModel.ticker == ticker, 
            OrderModel.status.in_([OrderStatus.NEW, OrderStatus.PARTIALLY_EXECUTED])
        ).with_for_update() 
        
        active_orders = (await session.exec(active_orders_stmt)).scalars().all()
        
        for order in active_orders:
            await async_cancel_order_and_unreserve(session, order)
        
        await session.delete(instrument)
        await session.commit()
        
        api_logger.info(
            f'Instrument permanently DELETED (cascaded) and {len(active_orders)} active orders cancelled by admin {admin_uuid_str}. Ticker: {ticker}'
        )
        return Ok()
    except HTTPException:
        raise
    except Exception as e:
        await session.rollback()
        api_logger.error(f'Critical error during instrument hard delete {ticker} by admin {admin_uuid_str}', exc_info=e)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error during instrument deletion.")