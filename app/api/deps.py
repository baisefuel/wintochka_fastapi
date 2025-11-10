from fastapi import Header, HTTPException, Depends, status
from typing import Optional
from sqlmodel import Session, select
from app.models.user import User as UserModel, UserRole
from app.core.db import get_session


def parse_token(auth_header: Optional[str]) -> Optional[str]:
    if not auth_header:
        return None
    parts = auth_header.split()
    if len(parts) == 2 and parts[0].upper() == "TOKEN":
        return parts[1]
    return None


def get_current_user(
    authorization: Optional[str] = Header(None),
    session: Session = Depends(get_session)
) -> UserModel:
    api_key = parse_token(authorization)
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, 
            detail="Invalid or missing API key"
        )
        
    user = session.exec(
        select(UserModel).where(UserModel.api_key == api_key, UserModel.is_active == True)
    ).first()
    
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, 
            detail="Invalid or missing API key"
        )
    return user


def get_current_admin(user: UserModel = Depends(get_current_user)) -> UserModel:
    if user.role != UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, 
            detail="Operation forbidden. Admin access required."
        )
    return user