"""FastAPI-app: webbui, schemaläggare och jobbarbetare i en process."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from ..config import output_root, secret_key
from ..db.session import init_db
from ..config import scheduler_enabled
from ..scheduler import shutdown_scheduler, start_scheduler
from .auth import require_login
from .dashboard_routes import router as dashboard_router
from .routes import router

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    init_db()
    output_root().mkdir(parents=True, exist_ok=True)
    if scheduler_enabled():
        start_scheduler()
    else:
        logger.info("Schemaläggaren är avstängd med PRENLY_NO_SCHEDULER")
    try:
        yield
    finally:
        shutdown_scheduler()


def create_app() -> FastAPI:
    app = FastAPI(title="prenly-dl", lifespan=lifespan)
    # Ordningen spelar roll: mellanvaror läggs på inifrån och ut, så
    # SessionMiddleware måste registreras SIST för att hamna ytterst och
    # hinna sätta request.session innan inloggningskontrollen läser den.
    app.middleware("http")(require_login)
    app.add_middleware(SessionMiddleware, secret_key=secret_key())
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(dashboard_router)
    app.include_router(router)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
