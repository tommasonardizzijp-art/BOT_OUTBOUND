"""Auth API: login, /me, user management (admin)."""
from datetime import datetime
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.config import settings
from app.models.user import User
from app.schemas.auth import (
    LoginRequest, TokenResponse, UserCreate, UserUpdate, UserResponse,
)
from app.utils.security import (
    hash_password, verify_password, create_access_token, TokenError,
)
from app.utils.auth_deps import get_current_user, require_admin


router = APIRouter(prefix="/auth", tags=["auth"])
users_router = APIRouter(prefix="/users", tags=["users"])


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for") if settings.auth_trust_forwarded_for else None
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def _rl_key(request: Request, email: str) -> str:
    ip = _client_ip(request)
    return f"loginrl:{ip}:{email.lower()}"


async def _check_login_rate_limit(request: Request, email: str) -> None:
    import redis.asyncio as aioredis

    r = aioredis.from_url(settings.redis_url, socket_connect_timeout=3, socket_timeout=3)
    try:
        key = await _rl_key(request, email)
        attempts = await r.incr(key)
        if attempts == 1:
            await r.expire(key, settings.auth_login_rate_limit_window_minutes * 60)
        if attempts > settings.auth_login_rate_limit_attempts:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Troppi tentativi di login. Riprova piu tardi.",
            )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Rate limit login non disponibile: {str(e)[:120]}",
        )
    finally:
        await r.aclose()


async def _clear_login_rate_limit(request: Request, email: str) -> None:
    import redis.asyncio as aioredis

    r = aioredis.from_url(settings.redis_url, socket_connect_timeout=3, socket_timeout=3)
    try:
        await r.delete(await _rl_key(request, email))
    finally:
        await r.aclose()


# ─────────────────────────────── Auth ─────────────────────────────────────

@router.post("/login", response_model=TokenResponse)
async def login(data: LoginRequest, request: Request, db: AsyncSession = Depends(get_db)):
    if not settings.jwt_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Auth disabled (JWT_SECRET not configured)",
        )

    await _check_login_rate_limit(request, data.email)

    user = await db.scalar(select(User).where(User.email == data.email))
    if not user or not verify_password(data.password, user.password_hash):
        # Same message for unknown email and wrong password — avoid enumeration.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User is disabled",
        )

    user.last_login_at = datetime.utcnow()
    await db.commit()
    await db.refresh(user)

    try:
        token = create_access_token(subject=user.id, role=user.role)
    except TokenError as e:
        raise HTTPException(status_code=500, detail=str(e))

    await _clear_login_rate_limit(request, data.email)

    return TokenResponse(
        access_token=token,
        expires_in=settings.jwt_expires_minutes * 60,
        user=UserResponse.model_validate(user),
    )


@router.get("/me", response_model=UserResponse)
async def me(current_user: Annotated[User, Depends(get_current_user)]):
    return UserResponse.model_validate(current_user)


# ──────────────────────────── User management ─────────────────────────────

@users_router.get("", response_model=list[UserResponse])
async def list_users(
    _: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    rows = (await db.execute(select(User).order_by(User.created_at.desc()))).scalars().all()
    return [UserResponse.model_validate(u) for u in rows]


@users_router.post("", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(
    data: UserCreate,
    _: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    existing = await db.scalar(select(User).where(User.email == data.email))
    if existing:
        raise HTTPException(status_code=409, detail="Email already in use")
    user = User(
        email=data.email,
        password_hash=hash_password(data.password),
        role=data.role,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return UserResponse.model_validate(user)


@users_router.patch("/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: str,
    data: UserUpdate,
    current_user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    user = await db.scalar(select(User).where(User.id == user_id))
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if data.role is not None:
        # Prevent locking yourself out by demoting the only admin.
        if user.id == current_user.id and data.role != "admin":
            raise HTTPException(status_code=400, detail="Cannot demote yourself")
        user.role = data.role
    if data.is_active is not None:
        if user.id == current_user.id and not data.is_active:
            raise HTTPException(status_code=400, detail="Cannot deactivate yourself")
        user.is_active = data.is_active
    if data.password is not None:
        user.password_hash = hash_password(data.password)

    await db.commit()
    await db.refresh(user)
    return UserResponse.model_validate(user)


@users_router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: str,
    current_user: Annotated[User, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    if user_id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")
    user = await db.scalar(select(User).where(User.id == user_id))
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    await db.delete(user)
    await db.commit()
