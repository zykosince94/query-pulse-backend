from __future__ import annotations

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from services.collector import build_demo_workload_snapshot, collector_status
from services.sources import (
    SourceConnectionRequest,
    SourceDefinition,
    SourceHealth,
    SOURCE_REGISTRY,
    SourceType,
    validate_connection_request,
)

app = FastAPI(title="Query Pulse API")
app.add_middleware(SessionMiddleware, secret_key="dev-secret-key-change-me")

templates = Jinja2Templates(directory="templates")


def _demo_sources() -> list[SourceDefinition]:
    return list(SOURCE_REGISTRY.values())


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/ready")
def ready():
    return {"status": "ready"}


@app.get("/sources")
def list_sources():
    return {"count": len(SOURCE_REGISTRY), "items": [source.model_dump() for source in SOURCE_REGISTRY.values()]}


@app.post("/sources/test")
def test_source_connection(payload: SourceConnectionRequest):
    try:
        validate_connection_request(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    message = f"{payload.type.value} connection check passed for {payload.name}"
    return {
        "status": "ok",
        "message": message,
        "engine": payload.type.value,
        "ready": True,
    }


@app.get("/sources/{source_id}/health")
def source_health(source_id: str):
    source = SOURCE_REGISTRY.get(source_id)
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")

    return SourceHealth(
        source_id=source.id,
        status="healthy",
        engine=source.type.value,
        ready=True,
        message=f"{source.name} is reachable and ready for collection.",
    ).model_dump()


@app.post("/sources")
def create_source(payload: SourceDefinition):
    if payload.id in SOURCE_REGISTRY:
        raise HTTPException(status_code=409, detail="Source already exists")

    SOURCE_REGISTRY[payload.id] = payload
    return {"status": "created", "source": payload.model_dump()}


@app.get("/collector/status")
def collector_state():
    return collector_status()


@app.get("/sources/{source_id}/workload-preview")
def workload_preview(source_id: str):
    return build_demo_workload_snapshot(source_id)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {})


@app.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    valid_users = {
        "admin": "admin123",
        "demo": "demo123",
    }

    if valid_users.get(username) != password:
        raise HTTPException(status_code=401, detail="Invalid username or password")

    request.session["user"] = username
    return RedirectResponse(url="/me", status_code=302)


@app.get("/me", response_class=HTMLResponse)
async def me_page(request: Request):
    username = request.session.get("user")
    if not username:
        return RedirectResponse(url="/login", status_code=302)

    is_demo = username == "demo"
    return templates.TemplateResponse(
        request,
        "me.html",
        {"username": username, "is_demo": is_demo},
    )


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)
