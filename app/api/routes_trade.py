import asyncio
from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import UUID4
from sqlalchemy import select
from typing import List, Dict, Union, Any, Optional
import logging

from sqlmodel.ext.asyncio.session import AsyncSession
from app.core.db import get_async_session 
from app.core.config import settings
from app.api.deps import get_current_user
from app.models.user import User as UserModel, UserBalance
from app.models.order import Order, Side
from app.schemas.openapi_schemas import (
    LimitOrderBody, MarketOrderBody, LimitOrder, MarketOrder,
    CreateOrderResponse, Ok, Direction
)
from app.api.match_engine import (
    async_try_to_match_order, 
    async_reserve_asset, 
    async_cancel_order_and_unreserve, 
    BalanceError
)

api_logger = logging.getLogger("api")

router = APIRouter(prefix="/api/v1", tags=["trade"]) 

DEFAULT_QUOTE_ASSET = settings.quote_asset


def create_validation_error_detail(loc: List[Union[str, int]], msg: str, error_type: str) -> Dict[str, Any]:
    return {
        "detail": [{
            "loc": loc,
            "msg": msg,
            "type": error_type
        }]
    }


@router.get("/balance", 
             response_model=Dict[str, int],
             summary="Get Balances",
             tags=["balance"])
async def get_balances(
    user: UserModel = Depends(get_current_user), 
    session: AsyncSession = Depends(get_async_session)
):
    await asyncio.sleep(0.5)
    user_uuid_str = str(user.uuid)
    try:
        balances = (await session.exec(
            select(UserBalance).where(UserBalance.user_uuid == user.uuid)
        )).scalars().all()
        
        result = {}
        for b in balances:
            available_val = b.available or 0
            reserved_val = b.reserved or 0
            
            total_balance = available_val + reserved_val
            
            result[b.ticker] = total_balance
        
        api_logger.info(
            f'User balances fetched. User ID: {user_uuid_str}, Total Balances: {result}'
        )
        
        return result
    except Exception as e:
        api_logger.error(f'Error fetching balances for user {user_uuid_str}', exc_info=e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, 
            detail="Internal error fetching balances."
        )

@router.post("/order", 
             response_model=CreateOrderResponse, 
             summary="Create Order",
             description="Создание лимитной или рыночной заявки. Атомарно исполняется, если есть ликвидность.",
             tags=["order"])
async def create_order(
    body: Union[LimitOrderBody, MarketOrderBody],
    user: UserModel = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session)
):
    is_limit_order = isinstance(body, LimitOrderBody)
    price = body.price if is_limit_order else 0 
    
    asset_to_reserve: str
    amount_to_reserve: int
    
    if body.direction == Direction.BUY:
        asset_to_reserve = DEFAULT_QUOTE_ASSET
        amount_to_reserve = body.qty * price if is_limit_order else 0 
    else:
        asset_to_reserve = body.ticker
        amount_to_reserve = body.qty
        
    order = Order(
        user_uuid=user.uuid,
        side=Side(body.direction.value),
        ticker=body.ticker,
        qty=body.qty,
        price=price if is_limit_order else None 
    )

    user_uuid_str = str(user.uuid)
    order_ticker = order.ticker
    order_type_str = "LIMIT" if is_limit_order else "MARKET"

    try:        
        session.add(order)
        await session.flush()
        await session.refresh(order)
        
        order_id = order.id 

        await async_try_to_match_order(session, order)

        await session.commit()
        
        api_logger.info(
            f'Order created and processed. Order ID: {order_id}, User ID: {user_uuid_str}, Ticker: {order_ticker}, Type: {order_type_str}'
        )
        
    except BalanceError as e:
        await session.rollback()
        api_logger.warning(
            f'Balance error for user {user_uuid_str}: {str(e)}. Asset: {asset_to_reserve}, Amount: {amount_to_reserve}'
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, 
            detail=create_validation_error_detail(
                loc=["body", "balance"],
                msg=str(e),
                error_type="balance_error"
            )
        )
    except Exception as e:
        await session.rollback()
        api_logger.error(
            f'Critical error processing order for user {user_uuid_str}', 
            exc_info=e
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, 
            detail="Error during order processing."
        )
        
    return CreateOrderResponse(order_id=order_id)


@router.get("/order", 
             response_model=List[Union[LimitOrder, MarketOrder]], 
             summary="List Orders",
             description="Просмотр списка активных и неактивных заявок пользователя.",
             tags=["order"])
async def list_orders(
    user: UserModel = Depends(get_current_user), 
    session: AsyncSession = Depends(get_async_session)
):
    user_uuid_str = str(user.uuid)
    
    try:
        orders = (await session.exec(
            select(Order).where(Order.user_uuid == user.uuid)
            .order_by(Order.timestamp.desc())
        )).scalars().all()
        
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
                
        api_logger.info(
            f'User orders listed. User ID: {user_uuid_str}, Count: {len(orders)}'
        )
        return result
        
    except Exception as e:
        api_logger.error(f'Error listing orders for user {user_uuid_str}', exc_info=e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, 
            detail="Error fetching order list."
        )


@router.get("/order/{order_id}", 
             response_model=Union[LimitOrder, MarketOrder], 
             summary="Get Order",
             description="Получение статуса заявки.",
             tags=["order"])
async def get_order(
    order_id: UUID4 = Path(..., title="Order Id"),
    user: UserModel = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session)
):
    user_uuid_str = str(user.uuid)
    
    try:
        order = (await session.exec(
            select(Order).where(Order.id == order_id, Order.user_uuid == user.uuid)
        )).scalars().first()
        
        if not order:
            api_logger.warning(
                f'Order not found or access denied. User ID: {user_uuid_str}, Order ID: {order_id}'
            )
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=create_validation_error_detail(
                    loc=["path", "order_id"],
                    msg="Order not found or access denied.",
                    error_type="not_found"
                )
            )
            
        body_data = {"direction": Direction(order.side.value), "ticker": order.ticker, "qty": order.qty}
        
        if order.price is not None:
            body = LimitOrderBody(price=order.price, **body_data)
            response = LimitOrder(
                id=order.id, status=order.status, user_id=order.user_uuid, 
                timestamp=order.timestamp, body=body, filled=order.filled
            )
        else:
            body = MarketOrderBody(**body_data)
            response = MarketOrder(
                id=order.id, status=order.status, user_id=order.user_uuid, 
                timestamp=order.timestamp, body=body
            )
            
        api_logger.info(
            f'Order details fetched. User ID: {user_uuid_str}, Order ID: {order_id}'
        )
        return response
        
    except HTTPException:
        raise
    except Exception as e:
        api_logger.error(f'Error fetching order {order_id} details for user {user_uuid_str}', exc_info=e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, 
            detail="Error fetching order details."
        )


@router.delete("/order/{order_id}", 
               response_model=Ok, 
               summary="Cancel Order",
               description="Отмена заявки и возврат зарезервированных средств.",
               tags=["order"])
async def cancel_order(
    order_id: UUID4 = Path(..., title="Order Id"),
    user: UserModel = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session)
):
    user_uuid_str = str(user.uuid)
    order: Optional[Order] = (await session.exec(
        select(Order).where(Order.id == order_id, Order.user_uuid == user.uuid).with_for_update() 
    )).scalars().first()
    
    if not order:
        api_logger.warning(
            f'Cancellation failed: Order not found or access denied. User ID: {user_uuid_str}, Order ID: {order_id}'
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=create_validation_error_detail(
                loc=["path", "order_id"],
                msg="Order not found or access denied.",
                error_type="not_found"
            )
        )
    order_id_str = str(order.id)
        
    try:
        await async_cancel_order_and_unreserve(session, order)

        await session.commit()
        
        api_logger.info(
            f'Order successfully cancelled. User ID: {user_uuid_str}, Order ID: {order_id_str}'
        )
        
    except BalanceError as e:
        await session.rollback()
        api_logger.warning(
            f'Cancellation failed due to BalanceError: {str(e)}. User ID: {user_uuid_str}, Order ID: {order_id_str}'
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, 
            detail=create_validation_error_detail(
                loc=["path", "order_id"],
                msg=str(e),
                error_type="cancel_error"
            )
        )
    except Exception as e:
        await session.rollback()
        api_logger.error(
            f'Critical error during cancellation of order {order_id_str} by user {user_uuid_str}', 
            exc_info=e
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, 
            detail="Error during order cancellation."
        )
        
    return Ok()