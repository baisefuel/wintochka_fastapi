from fastapi import APIRouter, Depends, HTTPException, Path
from sqlmodel import Session, select
from uuid import UUID

from app.core.db import get_session
from app.api.deps import get_current_admin
from app.models.user import User as UserModel, UserBalance
from app.models.instrument import Instrument as InstrumentModel
from app.schemas.openapi_schemas import (
    Ok, User, Instrument as InstrumentSchema, 
    Body_deposit_api_v1_admin_balance_deposit_post as DepositBody,
    Body_withdraw_api_v1_admin_balance_withdraw_post as WithdrawBody
)
from app.crud.balance import update_or_create_balance

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


@router.delete("/user/{user_id}", 
               response_model=User, 
               summary="Delete User",
               tags=["user"],
               description="Удаление пользователя (деактивация).")
def delete_user(user_id: UUID = Path(..., title="User Id"),
                admin_user: UserModel = Depends(get_current_admin),
                session: Session = Depends(get_session)):
    
    user = session.exec(select(UserModel).where(UserModel.uuid == user_id)).first()
    
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
        
    user.is_active = False
    session.add(user)
    session.commit()
    session.refresh(user)
    
    return User(id=user.uuid, name=user.name, role=user.role, api_key=user.api_key)


@router.post("/balance/deposit", 
             response_model=Ok, 
             summary="Deposit",
             tags=["balance"],
             description="Пополнение баланса пользователя.")
def deposit(body: DepositBody,
            admin_user: UserModel = Depends(get_current_admin), 
            session: Session = Depends(get_session)):
    
    user = session.exec(select(UserModel).where(UserModel.uuid == body.user_id)).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
        
    update_or_create_balance(session, body.user_id, body.ticker, body.amount)
    session.commit()
    return Ok()


@router.post("/balance/withdraw", 
             response_model=Ok, 
             summary="Withdraw",
             tags=["balance"],
             description="Списание доступных средств с баланса пользователя.")
def withdraw(body: WithdrawBody,
             admin_user: UserModel = Depends(get_current_admin), 
             session: Session = Depends(get_session)):
    
    user = session.exec(select(UserModel).where(UserModel.uuid == body.user_id)).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
        
    balance = session.exec(
        select(UserBalance).where(
            UserBalance.user_uuid == body.user_id, 
            UserBalance.ticker == body.ticker
        )
    ).first()
    
    if not balance or balance.available < body.amount:
        raise HTTPException(status_code=400, detail="Insufficient available balance or ticker not found.")
        
    update_or_create_balance(session, body.user_id, body.ticker, -body.amount)
    session.commit()
    return Ok()


@router.post("/instrument", 
             response_model=Ok, 
             summary="Add Instrument",
             description="Добавление нового торгового инструмента.")
def add_instrument(body: InstrumentSchema,
                   admin_user: UserModel = Depends(get_current_admin), 
                   session: Session = Depends(get_session)):
    
    existing = session.exec(select(InstrumentModel).where(InstrumentModel.ticker == body.ticker)).first()
    if existing:
        raise HTTPException(status_code=400, detail="Instrument with this ticker already exists.")
        
    instrument = InstrumentModel(name=body.name, ticker=body.ticker, is_active=True)
    session.add(instrument)
    session.commit()
    return Ok()


@router.delete("/instrument/{ticker}", 
               response_model=Ok, 
               summary="Delete Instrument",
               description="Делистинг (деактивация) инструмента.")
def delete_instrument(ticker: str = Path(..., title="Ticker"),
                      admin_user: UserModel = Depends(get_current_admin), 
                      session: Session = Depends(get_session)):
    
    instrument = session.exec(select(InstrumentModel).where(InstrumentModel.ticker == ticker)).first()
    
    if not instrument:
        raise HTTPException(status_code=404, detail="Instrument not found.")
        
    instrument.is_active = False
    session.add(instrument)
    session.commit()
    return Ok()