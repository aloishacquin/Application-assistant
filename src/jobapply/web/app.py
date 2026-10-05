"""FastAPI application: configuration, middlewares, lifecycle, routes."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session
from starlette.middleware.sessions import SessionMiddleware

from jobapply.config import AppConfig, ConfigError, load_config
from jobapply.ingest.fetch import fetch_offer_text
from jobapply.llm.client import AnthropicLLM, StructuredLLM
from jobapply.models.db import get_engine
from jobapply.tracking import service
from jobapply.web.auth import LoginThrottle, is_authenticated
from jobapply.web.routes import applications, auth, offers, pages, sources
from jobapply.web.templating import build_templates
from jobapply.worker import schedule_loop

STATIC_DIR = Path(__file__).parent / "static"
PUBLIC_PREFIXES = ("/login", "/static/", "/healthz")
SESSION_MAX_AGE = 30 * 24 * 3600


def create_app(
    cfg: AppConfig | None = None,
    *,
    llm_factory: Callable[[], StructuredLLM] | None = None,
    fetch: Callable[[str], str] = fetch_offer_text,
) -> FastAPI:
    cfg = cfg or load_config()
    secrets = cfg.secrets
    if secrets.app_password_hash is None or secrets.session_secret is None:
        raise ConfigError(
            "APP_PASSWORD_HASH et SESSION_SECRET doivent être définis dans .env "
            "(voir .env.example et `jobapply hash-password`)."
        )
    engine = get_engine(cfg.paths.db)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        with Session(engine) as session:
            service.reset_interrupted_analyses(session)
            service.reset_interrupted_generations(session)
        task = None
        if cfg.sources.schedule.enabled and cfg.sources.active:
            task = asyncio.create_task(schedule_loop(cfg, engine, app.state.llm_factory))
        yield
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title="JobApply SG", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.cfg = cfg
    app.state.engine = engine
    app.state.llm_factory = llm_factory or (lambda: AnthropicLLM.from_config(cfg))
    app.state.fetch = fetch
    app.state.templates = build_templates(cfg.settings.timezone)
    app.state.throttle = LoginThrottle()

    @app.middleware("http")
    async def require_login(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        path = request.url.path
        if not path.startswith(PUBLIC_PREFIXES) and not is_authenticated(request):
            if request.headers.get("HX-Request"):
                return Response(status_code=401, headers={"HX-Redirect": "/login"})
            return RedirectResponse(f"/login?next={quote(path)}", status_code=303)
        response = await call_next(request)
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    # Added last, so it wraps `require_login` and the session is available there.
    app.add_middleware(
        SessionMiddleware,
        secret_key=secrets.session_secret.get_secret_value(),
        session_cookie="jobapply_session",
        max_age=SESSION_MAX_AGE,
        same_site="lax",
        https_only=secrets.cookie_secure,
    )

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(auth.router)
    app.include_router(pages.router)
    app.include_router(offers.router)
    app.include_router(applications.router)
    app.include_router(sources.router)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
