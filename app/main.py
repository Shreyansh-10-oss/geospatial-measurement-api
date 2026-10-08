"""FastAPI application factory and lifecycle."""
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.files import router as files_router
from app.api.health import router as health_router
from app.config import Settings
from app.api.limits import UploadLimitsMiddleware
from app.utils.ownership import exclusive_workspace
from app.services.processing import recover_interrupted_processing
from app.db.session import (
    create_database_engine,
    create_session_factory,
    initialize_database,
)


def create_app(settings: Settings | None = None) -> FastAPI:
    configuration = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        with exclusive_workspace(configuration):
            engine = create_database_engine(configuration.database_url)
            try:
                initialize_database(engine)
                recover_interrupted_processing(engine, configuration)
                configuration.uploads_dir.mkdir(parents=True, exist_ok=True)
                application.state.engine = engine
                application.state.session_factory = create_session_factory(engine)
                yield
            finally:
                engine.dispose()

    application = FastAPI(title=configuration.app_name, lifespan=lifespan)
    application.state.settings = configuration
    application.add_middleware(UploadLimitsMiddleware, settings=configuration)
    application.include_router(health_router)
    application.include_router(files_router)
    return application


app = create_app()
