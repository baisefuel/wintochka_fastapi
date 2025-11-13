from fastapi import APIRouter, Depends, HTTPException, Path, status
from sqlalchemy import select
from sqlmodel.ext.asyncio.session import AsyncSession
from uuid import UUID
import logging
from typing import Union

from app.core.db import get_async_session
from app.api.deps import get_current_admin
from app.models.user import User as UserModel, UserBalance
from app.models.instrument import Instrument as InstrumentModel
from app.schemas.openapi_schemas import (
    Ok, User, Instrument as InstrumentSchema, 
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
                description="Удаление пользователя (деактивация).")
async def delete_user(
    user_id: UUID = Path(..., title="User Id"),
    admin_user: UserModel = Depends(get_current_admin),
    session: AsyncSession = Depends(get_async_session)
):
    try:
        user: Union[UserModel, None] = (await session.exec(select(UserModel).where(UserModel.uuid == user_id))).first()
        
        if not user:
            api_logger.warning(f'Admin {admin_user.uuid} failed to delete user: {user_id}. User not found.')
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
            
        user.is_active = False
        session.add(user)
        await session.commit()
        await session.refresh(user)
        
        api_logger.info(
            f'User successfully deactivated by admin {admin_user.uuid}. Target User ID: {user_id}'
        )
        
        return User(id=user.uuid, name=user.name, role=user.role, api_key=user.api_key)
        
    except HTTPException:
        raise
    except Exception as e:
        await session.rollback()
        api_logger.error(f'Critical error during user deletion for {user_id}', exc_info=e)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error during user deactivation.")


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
    try:
        user = (await session.exec(select(UserModel).where(UserModel.uuid == body.user_id))).first()
        if not user:
            api_logger.warning(f'Admin {admin_user.uuid} failed deposit to {body.user_id}: User not found.')
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
            
        await async_update_or_create_balance(session, body.user_id, body.ticker, body.amount)
        await session.commit()
        
        api_logger.info(
            f'Deposit successful by admin {admin_user.uuid}. Target User ID: {body.user_id}, Ticker: {body.ticker}, Amount: {body.amount}'
        )
        
        return Ok()
    except HTTPException:
        raise
    except Exception as e:
        await session.rollback()
        api_logger.error(
            f'Critical error during deposit for {body.user_id} of {body.amount} {body.ticker}', 
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
    try:
        user = (await session.exec(select(UserModel).where(UserModel.uuid == body.user_id))).first()
        if not user:
            api_logger.warning(f'Admin {admin_user.uuid} failed withdraw from {body.user_id}: User not found.')
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
            
        balance = (await session.exec(
            select(UserBalance).where(
                UserBalance.user_uuid == body.user_id, 
                UserBalance.ticker == body.ticker
            ).with_for_update()
        )).first()
        
        if not balance or balance.available < body.amount:
            api_logger.warning(
                f'Admin {admin_user.uuid} failed withdraw from {body.user_id}: Insufficient balance. Ticker: {body.ticker}, Requested: {body.amount}, Available: {balance.available if balance else 0}'
            )
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, 
                detail="Insufficient available balance or ticker not found."
            )
            
        await async_update_or_create_balance(session, body.user_id, body.ticker, -body.amount)
        await session.commit()
        
        api_logger.info(
            f'Withdrawal successful by admin {admin_user.uuid}. Target User ID: {body.user_id}, Ticker: {body.ticker}, Amount: {body.amount}'
        )
        
        return Ok()
    except HTTPException:
        raise
    except Exception as e:
        await session.rollback()
        api_logger.error(
            f'Critical error during withdraw for {body.user_id} of {body.amount} {body.ticker}', 
            exc_info=e
        )
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error during balance withdrawal.")


@router.post("/instrument", 
              response_model=Ok, 
              summary="Add Instrument",
              description="Добавление нового торгового инструмента.")
async def add_instrument(
    body: InstrumentSchema,
    admin_user: UserModel = Depends(get_current_admin), 
    session: AsyncSession = Depends(get_async_session)
):
    try:
        existing = (await session.exec(select(InstrumentModel).where(InstrumentModel.ticker == body.ticker))).first()
        if existing:
            api_logger.warning(f'Admin {admin_user.uuid} failed to add instrument: {body.ticker} already exists.')
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Instrument with this ticker already exists.")
            
        instrument = InstrumentModel(name=body.name, ticker=body.ticker, is_active=True)
        session.add(instrument)
        await session.commit()
        
        api_logger.info(
            f'Instrument successfully added by admin {admin_user.uuid}. Ticker: {body.ticker}, Name: {body.name}'
        )
        
        return Ok()
    except HTTPException:
        raise
    except Exception as e:
        await session.rollback()
        api_logger.error(f'Critical error adding instrument {body.ticker}', exc_info=e)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error adding instrument.")


@router.delete("/instrument/{ticker}", 
                response_model=Ok, 
                summary="Delete Instrument",
                description="Делистинг (деактивация) инструмента.")
async def delete_instrument(
    ticker: str = Path(..., title="Ticker"),
    admin_user: UserModel = Depends(get_current_admin), 
    session: AsyncSession = Depends(get_async_session)
):
    try:
        instrument: Union[InstrumentModel, None] = (await session.exec(select(InstrumentModel).where(InstrumentModel.ticker == ticker))).first()
        
        if not instrument:
            api_logger.warning(f'Admin {admin_user.uuid} failed to delete instrument: {ticker} not found.')
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Instrument not found.")
            
        instrument.is_active = False
        session.add(instrument)
        await session.commit()
        
        api_logger.info(
            f'Instrument successfully deactivated by admin {admin_user.uuid}. Ticker: {ticker}'
        )
        
        return Ok()
    except HTTPException:
        raise
    except Exception as e:
        await session.rollback()
        api_logger.error(f'Critical error deleting instrument {ticker}', exc_info=e)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error deleting instrument.")