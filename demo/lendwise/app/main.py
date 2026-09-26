import logging
import os

from fastapi import Depends, FastAPI
from fastapi.routing import APIRouter

from .auth import get_current_user, require_mfa
from .auth_routes import router as auth_router
from .database import engine
from .logging_mw import RequestBodyLoggingMiddleware
from .models import Base
from .routers.applications import router as applications_router
from .routers.servicing import router as servicing_router
from .support_legacy import router as legacy_router

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
_handlers = [logging.StreamHandler()]

_log_dir = os.path.join(os.path.dirname(__file__), "..", "var", "log")
_log_file = os.path.join(_log_dir, "app.log")
try:
    os.makedirs(_log_dir, exist_ok=True)
    _handlers.append(logging.FileHandler(_log_file))
except OSError:
    pass  # Running in a read-only or temp environment

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=_handlers,
)
logger = logging.getLogger("lendwise")

# Create tables on startup (dev convenience)
Base.metadata.create_all(bind=engine)

# ── Primary application ────────────────────────────────────────────────────────
app = FastAPI(title="Lendwise API", version="1.0.0")
app.add_middleware(RequestBodyLoggingMiddleware)

# Public routes (no auth)
app.include_router(auth_router)


@app.get("/health", tags=["ops"])
def health():
    return {"status": "ok"}


# Authenticated API router (all routes require MFA)
api = APIRouter(prefix="/api/v1", dependencies=[Depends(require_mfa)])
api.include_router(applications_router)
api.include_router(servicing_router)
app.include_router(api)

# ── Mobile token endpoint (no MFA dependency on the router) ───────────────────
mobile_router = APIRouter(prefix="/api/v1")


@mobile_router.get("/profile", dependencies=[Depends(get_current_user)])
def mobile_profile(user: dict = Depends(get_current_user)):
    return {"sub": user.get("sub"), "customer_id": user.get("customer_id")}


app.include_router(mobile_router)

# ── Legacy support sub-app (separate mount, no require_mfa) ──────────────────
support_app = FastAPI(title="Lendwise Internal Support")
support_app.include_router(legacy_router)

app.mount("/internal/v1/support", support_app)
