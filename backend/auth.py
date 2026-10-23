"""Single-owner authentication; credentials and revocable sessions stay on this machine."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse

from .settings import DATA_DIR

COOKIE = "h3_session"


class Credentials(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=6, max_length=256)
    remember: bool = False


class AuthStore:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.failures: dict[str, list[float]] = {}

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("CREATE TABLE IF NOT EXISTS owner (id INTEGER PRIMARY KEY CHECK(id=1), username TEXT NOT NULL, salt TEXT NOT NULL, digest TEXT NOT NULL)")
        connection.execute("CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, expires REAL NOT NULL)")
        connection.commit()
        return connection

    def configured(self):
        with closing(self.connect()) as connection:
            return connection.execute("SELECT 1 FROM owner").fetchone() is not None

    @staticmethod
    def digest(password: str, salt: str):
        return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()

    def setup(self, credentials: Credentials):
        salt = secrets.token_hex(16)
        digest = self.digest(credentials.password, salt)
        with closing(self.connect()) as connection, connection:
            try:
                connection.execute("INSERT INTO owner VALUES (1, ?, ?, ?)", (credentials.username.strip(), salt, digest))
            except sqlite3.IntegrityError:
                raise HTTPException(409, "管理员已创建，请登录") from None

    def login(self, credentials: Credentials, client: str):
        # Reserve the attempt before password hashing so concurrent requests cannot bypass the limit.
        with self.lock:
            now = time.time()
            self.failures = {key: [t for t in times if now - t < 300] for key, times in self.failures.items() if any(now - t < 300 for t in times)}
            attempts = self.failures.setdefault(client, [])
            if len(attempts) >= 5:
                raise HTTPException(429, "尝试过于频繁，请五分钟后重试")
            attempts.append(now)
        with closing(self.connect()) as connection:
            owner = connection.execute("SELECT username, salt, digest FROM owner").fetchone()
        candidate = self.digest(credentials.password, owner[1] if owner else "00" * 16)
        if not owner or not hmac.compare_digest(candidate, owner[2]) or credentials.username.strip() != owner[0]:
            raise HTTPException(401, "账号或密码错误")
        with self.lock:
            self.failures.pop(client, None)

    def issue(self, remember: bool):
        token = secrets.token_urlsafe(32)
        ttl = (30 * 24 if remember else 12) * 3600
        with closing(self.connect()) as connection, connection:
            connection.execute("DELETE FROM sessions WHERE expires <= ?", (time.time(),))
            connection.execute("INSERT INTO sessions VALUES (?, ?)", (hashlib.sha256(token.encode()).hexdigest(), time.time() + ttl))
        return token, ttl

    def valid(self, token: str):
        if not token or len(token) > 128:
            return False
        with closing(self.connect()) as connection:
            return connection.execute("SELECT 1 FROM sessions WHERE token=? AND expires>?", (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone() is not None

    def revoke(self, token: str):
        with closing(self.connect()) as connection, connection:
            connection.execute("DELETE FROM sessions WHERE token=?", (hashlib.sha256(token.encode()).hexdigest(),))


store = AuthStore(DATA_DIR / "auth.db")
router = APIRouter(prefix="/api/auth")


def same_origin(scope):
    headers = dict(scope.get("headers", []))
    origin = headers.get(b"origin")
    if origin:
        parsed = urlparse(origin.decode())
        return parsed.scheme in {"http", "https"} and parsed.netloc.lower() == headers.get(b"host", b"").decode().lower()
    return headers.get(b"sec-fetch-site") != b"cross-site"


class AuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        # The desktop launcher probes this boolean-only endpoint before a user can log in.
        public_paths = {"/api/auth/status", "/api/auth/login", "/api/auth/setup", "/api/system/project-status"}
        protected = path.startswith("/api/") and path not in public_paths
        protected = protected or path in {"/docs", "/redoc", "/openapi.json", "/assets/fixed-reference.png"}
        request = Request(scope) if scope["type"] == "http" else None
        from starlette.requests import HTTPConnection
        connection = request or HTTPConnection(scope)
        unsafe = scope["type"] == "websocket" or scope.get("method") not in {"GET", "HEAD", "OPTIONS"}
        status = 403 if unsafe and not same_origin(scope) else 401 if protected and not store.valid(connection.cookies.get(COOKIE, "")) else None
        if status:
            if scope["type"] == "websocket":
                return await send({"type": "websocket.close", "code": 4403 if status == 403 else 4401})
            return await JSONResponse({"detail": "请先登录" if status == 401 else "请求来源不受信任"}, status_code=status)(scope, receive, send)
        await self.app(scope, receive, send)


@router.get("/status")
def status(request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return {"configured": store.configured(), "authenticated": store.valid(request.cookies.get(COOKIE, ""))}


def session_response(credentials: Credentials, request: Request, response: Response):
    token, ttl = store.issue(credentials.remember)
    response.set_cookie(COOKIE, token, max_age=ttl if credentials.remember else None, httponly=True, secure=request.url.scheme == "https", samesite="strict", path="/")
    response.headers["Cache-Control"] = "no-store"
    return {"authenticated": True}


@router.post("/setup")
def setup(credentials: Credentials, request: Request, response: Response):
    if not request.client or request.client.host not in {"127.0.0.1", "::1", "localhost", "testclient"}:
        raise HTTPException(403, "请在本机创建管理员")
    if not credentials.username.strip():
        raise HTTPException(422, "请输入账号")
    store.setup(credentials)
    return session_response(credentials, request, response)


@router.post("/login")
def login(credentials: Credentials, request: Request, response: Response):
    store.login(credentials, request.client.host if request.client else "unknown")
    return session_response(credentials, request, response)


@router.post("/logout")
def logout(request: Request, response: Response):
    store.revoke(request.cookies.get(COOKIE, ""))
    response.delete_cookie(COOKIE, path="/")
    return {"authenticated": False}
