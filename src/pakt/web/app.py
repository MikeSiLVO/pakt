"""FastAPI application for Pakt web interface."""

from __future__ import annotations

import asyncio
import ipaddress
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from pakt import __version__
from pakt.config import Config, get_config_dir
from pakt.web import state
from pakt.web.api import plex, servers, status, sync, trakt
from pakt.web.auth import (
    SESSION_COOKIE,
    SESSION_SECONDS,
    LoginThrottle,
    check_password,
    is_local,
    make_session,
    session_valid,
)

PUBLIC_PATHS = ("/login", "/favicon.ico", "/assets/")
MAX_LOGIN_BODY = 4096


def _host_allowed(host: str, allowed_hosts: list[str]) -> bool:
    """True when the Host header is localhost, an IP address, or a name the user allowed."""
    try:
        name = urlsplit(f"//{host}").hostname
    except ValueError:
        return False
    if not name:
        return False
    if name == "localhost" or name in allowed_hosts:
        return True
    # IP literals pass; DNS rebinding only works through a hostname
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the scheduler with the app when config enables it, and stop it on shutdown."""
    config = Config.load()
    if config.scheduler.enabled and config.scheduler.interval_hours > 0:
        sync.start_scheduler(config)

    yield

    if state.scheduler:
        state.scheduler.stop()


def create_app() -> FastAPI:
    """Create the FastAPI application with its host, origin and login checks and all routes."""
    app = FastAPI(
        title="Pakt",
        description="Plex-Trakt sync",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    # pending Trakt device code kept server-side; a page never supplies its own
    app.state.trakt_device = {}
    app.state.plex_pins = {}
    for api in (status, sync, trakt, plex, servers):
        app.include_router(api.router)

    @app.middleware("http")
    async def reject_foreign_requests(request: Request, call_next):
        """Reject rebound hosts and cross-site writes, require remote logins, and block framing."""
        host = request.headers.get("host", "")
        web = state.load_config_cached().web
        allowed_hosts = [h.lower() for h in web.allowed_hosts]
        if not _host_allowed(host, allowed_hosts):
            return PlainTextResponse(
                f"Host '{host}' is not allowed. Add it to web.allowed_hosts in config.json.", status_code=403
            )
        origin = request.headers.get("origin")
        if request.method not in ("GET", "HEAD", "OPTIONS") and origin:
            parts = urlsplit(origin)
            if parts.netloc != host and parts.hostname not in allowed_hosts:
                return PlainTextResponse(
                    f"Request from '{origin}' refused. Add its host name to web.allowed_hosts in config.json.",
                    status_code=403,
                )
        request.state.local = is_local(request.client.host if request.client else None, host, request.headers)
        path = request.url.path
        if not request.state.local and not path.startswith(PUBLIC_PATHS):
            if not session_valid(request.cookies.get(SESSION_COOKIE, ""), web.session_secret, web.password_hash):
                if path.startswith("/api/"):
                    return JSONResponse({"detail": "Log in first"}, status_code=401)
                return RedirectResponse("/login", status_code=303)
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    template_dir = Path(__file__).parent / "templates"
    templates = Jinja2Templates(directory=str(template_dir))
    assets_dir = Path(__file__).parent.parent / "assets"

    @app.get("/favicon.ico")
    async def favicon():
        """Serve favicon."""
        icon_path = assets_dir / "icon.png"
        if icon_path.exists():
            return FileResponse(icon_path, media_type="image/png")
        return FileResponse(assets_dir / "icon.svg", media_type="image/svg+xml")

    @app.get("/assets/{filename}")
    async def serve_asset(filename: str):
        """Serve static assets."""
        file_path = (assets_dir / filename).resolve()
        if not file_path.is_relative_to(assets_dir.resolve()):
            return HTMLResponse(status_code=404, content="Not found")
        if file_path.exists() and file_path.is_file():
            media_type = "image/png" if filename.endswith(".png") else "image/svg+xml"
            return FileResponse(file_path, media_type=media_type)
        return HTMLResponse(status_code=404, content="Not found")

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        """Render the main dashboard."""
        config = Config.load()
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "config": config,
                "sync_state": state.sync_state,
                "config_dir": str(get_config_dir()),
                "version": __version__,
                "remote": not request.state.local,
            },
        )

    throttle = LoginThrottle()

    def login_page(request: Request, error: str | None = None, status_code: int = 200) -> HTMLResponse:
        """Render the login form, or the steps to set a password when none is set."""
        web = state.load_config_cached().web
        context = {"password_set": bool(web.password_hash and web.session_secret), "error": error}
        return templates.TemplateResponse(request, "login.html", context, status_code=status_code)

    @app.get("/login", response_class=HTMLResponse)
    async def show_login(request: Request):
        """Show the login page to a visitor from another device."""
        if request.state.local:
            return RedirectResponse("/", status_code=303)
        return login_page(request)

    @app.post("/login")
    async def login(request: Request):
        """Check the password and start a session, locking an address out after repeated misses."""
        web = Config.load().web
        address = request.client.host if request.client else ""
        if not (web.password_hash and web.session_secret):
            return login_page(request, status_code=403)
        if throttle.locked(address):
            return login_page(request, "Too many attempts. Wait a minute and try again.", 429)
        if int(request.headers.get("content-length") or 0) > MAX_LOGIN_BODY:
            return login_page(request, "Request too large.", 413)
        form = parse_qs((await request.body()).decode(errors="replace"))
        if not await asyncio.to_thread(check_password, form.get("password", [""])[0], web.password_hash):
            throttle.record_failure(address)
            return login_page(request, "Wrong password.", 401)
        throttle.clear(address)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            SESSION_COOKIE, make_session(web.session_secret, web.password_hash), max_age=SESSION_SECONDS,
            httponly=True, samesite="strict",
            secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https",
        )
        return response

    @app.post("/logout")
    async def logout():
        """End the session and return to the login page."""
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE)
        return response

    @app.post("/api/shutdown")
    async def shutdown_server() -> dict[str, str]:
        """Shutdown the web server."""
        async def do_shutdown():
            """Let the HTTP response flush before killing the process."""
            await asyncio.sleep(0.5)
            os._exit(0)

        asyncio.create_task(do_shutdown())
        return {"status": "ok", "message": "Server shutting down..."}

    return app
