"""Login and logout."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from jobapply.web.auth import LoginThrottle, safe_next, verify_csrf, verify_password
from jobapply.web.templating import render

router = APIRouter()


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/") -> Response:
    if request.session.get("authenticated"):
        return RedirectResponse(safe_next(next), status_code=303)
    return render(request, "login.html", next=safe_next(next))


@router.post("/login")
def login(
    request: Request,
    password: Annotated[str, Form()],
    next: Annotated[str, Form()] = "/",
) -> Response:
    throttle: LoginThrottle = request.app.state.throttle
    key = _client_key(request)
    if throttle.is_blocked(key):
        return render(
            request,
            "login.html",
            status_code=429,
            next=safe_next(next),
            error="Trop de tentatives : réessaie dans quelques minutes.",
        )
    stored = request.app.state.cfg.secrets.app_password_hash.get_secret_value()
    if not verify_password(password, stored):
        throttle.record_failure(key)
        return render(
            request,
            "login.html",
            status_code=401,
            next=safe_next(next),
            error="Mot de passe incorrect.",
        )
    throttle.reset(key)
    request.session.clear()  # new session: no fixation
    request.session["authenticated"] = True
    return RedirectResponse(safe_next(next), status_code=303)


@router.post("/logout", dependencies=[Depends(verify_csrf)])
def logout(request: Request) -> Response:
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
