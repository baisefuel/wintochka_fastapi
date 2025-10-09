from app.core.db import get_session

def get_db():
    return next(get_session())
