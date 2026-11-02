"""Remote run-status API for Coolify (HTTPS terminated at the proxy)."""

from __future__ import annotations

import logging
import os
import tempfile
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock

import bcrypt
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("run_status")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    admin_password_hash: str = Field(
        ...,
        description="bcrypt hash of the admin password (set ADMIN_PASSWORD_HASH).",
    )
    data_dir: Path = Path("/data")
    status_filename: str = "run_status.txt"
    default_status: int = 0
    set_rate_limit: int = 10
    set_rate_window_seconds: int = 60

    @property
    def status_path(self) -> Path:
        return self.data_dir / self.status_filename


settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)


class SetRunStatusRequest(BaseModel):
    status: int = Field(..., description="Integer status to persist.")
    password: str = Field(..., min_length=1, max_length=256)


class RateLimiter:
    """Simple per-IP sliding window for the privileged set endpoint."""

    def __init__(self, max_calls: int, window_seconds: int) -> None:
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def check(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            bucket = self._hits[key]
            cutoff = now - self.window_seconds
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= self.max_calls:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Too many requests",
                )
            bucket.append(now)


rate_limiter = RateLimiter(settings.set_rate_limit, settings.set_rate_window_seconds)


def _read_status() -> int:
    path = settings.status_path
    if not path.exists():
        return settings.default_status
    try:
        raw = path.read_text(encoding="utf-8").strip()
        return int(raw)
    except (OSError, ValueError) as exc:
        logger.error("Failed to read status file %s: %s", path, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to read run status",
        ) from exc


def _write_status(value: int) -> None:
    """Atomically write status so readers never see a partial file."""
    path = settings.status_path
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".run_status_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(str(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        try:
            os.chmod(path, 0o640)
        except OSError:
            pass
    except OSError as exc:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        logger.error("Failed to write status file %s: %s", path, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to save run status",
        ) from exc


def verify_password(password: str) -> None:
    """Constant-time bcrypt verify; never leak hash details in responses."""
    try:
        stored = settings.admin_password_hash.encode("utf-8")
        candidate = password.encode("utf-8")
        if len(candidate) > 72:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid credentials",
            )
        ok = bcrypt.checkpw(candidate, stored)
    except HTTPException:
        raise
    except (ValueError, TypeError) as exc:
        logger.error("Password hash configuration is invalid: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server authentication misconfigured",
        ) from exc

    if not ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
        )


def client_ip(request: Request) -> str:
    if settings.set_rate_limit and request.client:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.client.host
    return "unknown"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if not settings.status_path.exists():
        _write_status(settings.default_status)
        logger.info(
            "Initialized status file at %s with %s",
            settings.status_path,
            settings.default_status,
        )
    yield


app = FastAPI(title="Remote Runs Trigger", version="1.0.0", lifespan=lifespan)


@app.get("/health", response_class=PlainTextResponse)
def health() -> str:
    return "ok"


@app.get("/check_run_status", response_class=PlainTextResponse)
def check_run_status() -> str:
    """Return the current integer run status (plain text)."""
    return str(_read_status())


@app.post("/set_run_status")
def set_run_status(request: Request, payload: SetRunStatusRequest) -> dict[str, int]:
    """Persist a new integer status after password verification."""
    rate_limiter.check(client_ip(request))
    verify_password(payload.password)
    _write_status(payload.status)
    logger.info("Run status updated to %s", payload.status)
    return {"status": payload.status}
