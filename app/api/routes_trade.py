from fastapi import APIRouter, Depends, HTTPException, Path, status
from sqlmodel import Session, select
from typing import List, Dict, Union
from uuid import UUID
from app.core.db import get_session
from app.core.config import settings
from app.api.deps import get_current_user
from app.models.user import User as UserModel, UserBalance
from app.models.order import Order, Side
from app.schemas.openapi_schemas import (
    LimitOrderBody, MarketOrderBody, LimitOrder, MarketOrder,
    CreateOrderResponse, Ok, Direction, OrderStatus
)
from app.api.match_engine import (
    try_to_match_order, 
    reserve_asset, 
    cancel_order_and_unreserve, 
    BalanceError
)

router = APIRouter(prefix="/api/v1")

DEFAULT_QUOTE_ASSET = settings.quote_asset

@router.get("/balance", 
            response_model=Dict[str, int], 
            summary="Get Balances",
            tags=["balance"])
def get_balances(user: UserModel = Depends(get_current_user), 
                 session: Session = Depends(get_session)):
    
    balances = session.exec(
        select(UserBalance).where(UserBalance.user_uuid == user.uuid)
    ).all()
    
    result = {b.ticker: b.available for b in balances}
    return result


@router.post("/order", 
             response_model=CreateOrderResponse, 
             summary="Create Order",
             description="Создание лимитной или рыночной заявки. Атомарно исполняется, если есть ликвидность.",
             tags=["order"])
def create_order(
    body: Union[LimitOrderBody, MarketOrderBody],
    user: UserModel = Depends(get_current_user),
    session: Session = Depends(get_session)
):
    is_limit_order = isinstance(body, LimitOrderBody)
    price = body.price if is_limit_order else 0
    
    asset_to_reserve = ""
    amount_to_reserve = 0
    if body.direction == Direction.BUY:
        asset_to_reserve = DEFAULT_QUOTE_ASSET
        if is_limit_order: amount_to_reserve = body.qty * price 
        else: amount_to_reserve = 1
    else:
        asset_to_reserve = body.ticker
        amount_to_reserve = body.qty
        
    try:
        if is_limit_order or body.direction == Direction.SELL:
            reserve_asset(session, user.uuid, asset_to_reserve, amount_to_reserve)
            session.commit()
    except BalanceError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        
    order = Order(
        user_uuid=user.uuid,
        side=Side(body.direction.value),
        ticker=body.ticker,
        qty=body.qty,
        price=price if is_limit_order else None
    )
    
    session.add(order)
    session.commit()
    session.refresh(order)

    try:
        try_to_match_order(session, order)
    except Exception as e:
        print(f"Error during order matching: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error during order matching.")
    
    session.commit()

    return CreateOrderResponse(order_id=order.id)


@router.get("/order", 
            response_model=List[Union[LimitOrder, MarketOrder]], 
            summary="List Orders",
            description="Просмотр списка активных и неактивных заявок пользователя.",
            tags=["order"])
def list_orders(user: UserModel = Depends(get_current_user), 
                session: Session = Depends(get_session)):
    
    orders = session.exec(
        select(Order).where(Order.user_uuid == user.uuid)
        .order_by(Order.timestamp.desc())
    ).all()
    
    result = []
    for o in orders:
        body_data = {"direction": Direction(o.side.value), "ticker": o.ticker, "qty": o.qty}
        
        if o.price is not None:
            body = LimitOrderBody(price=o.price, **body_data)
            result.append(LimitOrder(
                id=o.id, status=o.status, user_id=o.user_uuid, 
                timestamp=o.timestamp, body=body, filled=o.filled
            ))
        else:
            body = MarketOrderBody(**body_data)
            result.append(MarketOrder(
                id=o.id, status=o.status, user_id=o.user_uuid, 
                timestamp=o.timestamp, body=body
            ))
            
    return result


@router.get("/order/{order_id}", 
            response_model=Union[LimitOrder, MarketOrder], 
            summary="Get Order",
            description="Получение статуса заявки.",
            tags=["order"])
def get_order(order_id: UUID = Path(..., title="Order Id"),
              user: UserModel = Depends(get_current_user),
              session: Session = Depends(get_session)):
    
    order = session.exec(
        select(Order).where(Order.id == order_id, Order.user_uuid == user.uuid)
    ).first()
    
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
        
    body_data = {"direction": Direction(order.side.value), "ticker": order.ticker, "qty": order.qty}
    
    if order.price is not None:
        body = LimitOrderBody(price=order.price, **body_data)
        return LimitOrder(
            id=order.id, status=order.status, user_id=order.user_uuid, 
            timestamp=order.timestamp, body=body, filled=order.filled
        )
    else:
        body = MarketOrderBody(**body_data)
        return MarketOrder(
            id=order.id, status=order.status, user_id=order.user_uuid, 
            timestamp=order.timestamp, body=body
        )


@router.delete("/order/{order_id}", 
               response_model=Ok, 
               summary="Cancel Order",
               description="Отмена заявки и возврат зарезервированных средств.",
               tags=["order"])
def cancel_order(order_id: UUID = Path(..., title="Order Id"),
                 user: UserModel = Depends(get_current_user),
                 session: Session = Depends(get_session)):
    
    order = session.exec(
        select(Order).where(Order.id == order_id, Order.user_uuid == user.uuid)
    ).first()
    
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
        
    try:
        cancel_order_and_unreserve(session, order)
        session.commit()
    except BalanceError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        
    return Ok()