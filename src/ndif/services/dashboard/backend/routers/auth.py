"""Login / logout / whoami."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel

from ..auth import (
    COOKIE_NAME,
    issue_session_token,
    require_auth,
    verify_password,
)
from ..config import Settings, get_settings


router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    username: str


class MeResponse(BaseModel):
    username: str
    dev_mode: bool


@router.post("/login", response_model=LoginResponse)
def login(
    payload: LoginRequest,
    response: Response,
    settings: Settings = Depends(get_settings),
) -> LoginResponse:
    if payload.username != settings.username or not verify_password(
        payload.password, settings.password_hash
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )

    token = issue_session_token(settings.username, settings)
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=settings.session_ttl_days * 24 * 60 * 60,
        httponly=True,
        samesite="lax",
        secure=False,  # set True behind HTTPS
        path="/",
    )
    return LoginResponse(username=settings.username)


@router.post("/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/me", response_model=MeResponse)
def me(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> MeResponse:
    if settings.dev_mode:
        return MeResponse(username=settings.username, dev_mode=True)
    username = require_auth(request)
    return MeResponse(username=username, dev_mode=False)
