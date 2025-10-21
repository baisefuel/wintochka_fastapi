from fastapi import FastAPI
from app.core.db import init_db
from app.api.routes_orders import router as orders_router


app = FastAPI(title="Wintochka", version="0.2")

@app.on_event("startup")
def on_startup():
    init_db()

app.include_router(orders_router)

@app.get("/health", tags=["System"])
def health():
    return {"status": "ok"}