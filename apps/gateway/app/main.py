import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from tortoise.contrib.fastapi import RegisterTortoise

from app.config import settings
from app.db import TORTOISE_CONFIG
from app.obs.middleware import RequestIDMiddleware
from app.routes.agent import router as agent_router
from app.routes.audit import router as audit_router
from app.routes.health import router as health_router
from app.routes.residents import router as residents_router
from app.routes.transcribe import router as transcribe_router
from app.seeds.seed import seed_if_empty

logging.basicConfig(level=settings.LOG_LEVEL)
log = logging.getLogger("dexter-mini")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("startup: initialising Tortoise ORM")
    async with RegisterTortoise(
        app=app,
        config=TORTOISE_CONFIG,
        generate_schemas=True,
        add_exception_handlers=False,
    ):
        if settings.AUTO_SEED:
            await seed_if_empty()
        log.info("startup: ready")
        yield
        log.info("shutdown: closing DB connections")


app = FastAPI(title="dexter-mini gateway", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://localhost:3000",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-Id"],
)
app.add_middleware(RequestIDMiddleware)
app.include_router(health_router)
app.include_router(agent_router)
app.include_router(residents_router)
app.include_router(audit_router)
app.include_router(transcribe_router)


@app.get("/")
async def root() -> dict:
    return {"service": "dexter-mini gateway", "version": "0.1.0"}
