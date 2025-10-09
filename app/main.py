from fastapi import FastAPI
from app.core.db import init_db

app = FastAPI(title="Wintochka", version="0.1")

@app.on_event("startup")
def on_startup():
    init_db()

@app.get("/health", tags=["System"])
def health():
    return {"status": "ok"}