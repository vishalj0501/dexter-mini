import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from tortoise import Tortoise

from app.config import settings
from app.db import TORTOISE_CONFIG
from app.routes.health import router as health_router
from app.seeds.seed import seed_if_empty

logging.basicConfig(level=settings.LOG_LEVEL)
log = logging.getLogger("dexter-mini")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("startup: initialising Tortoise ORM")
    await Tortoise.init(config=TORTOISE_CONFIG)
    await Tortoise.generate_schemas(safe=True)
    if settings.AUTO_SEED:
        await seed_if_empty()
    log.info("startup: ready")
    try:
        yield
    finally:
        log.info("shutdown: closing DB connections")
        await Tortoise.close_connections()


app = FastAPI(title="dexter-mini gateway", version="0.1.0", lifespan=lifespan)
app.include_router(health_router)


@app.get("/")
async def root() -> dict:
    return {"service": "dexter-mini gateway", "version": "0.1.0"}
