import hmac

from fastapi import APIRouter, HTTPException, Request, Response
from starlette.datastructures import MutableHeaders

from app.core.session import AUTH_COOKIE_NAME
from app.schemas.auth import (
    ActivationRequest,
    AuthResponse,
    ChangePasswordRequest,
    ForgotPasswordRequest,
    LoginRequest,
    PublicUser,
    RegisterRequest,
    ResetPasswordRequest,
)


router = APIRouter(prefix="/auth", tags=["auth"])


def public_user(user: dict) -> PublicUser:
    return PublicUser(
        id=user["id"],
        email=user["email"],
        display_name=user["display_name"],
        role=user["role"],
        active=user["active"],
        email_verified=user.get("email_verified", False),
    )


def get_auth_session(request: Request) -> dict | None:
    session_id = request.cookies.get(AUTH_COOKIE_NAME)
    if not session_id:
        return None
    return request.app.state.chat_repository.get_auth_session(session_id)


def require_user(request: Request) -> dict:
    auth_session = get_auth_session(request)
    if auth_session is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    request.state.user_id = auth_session["user_id"]
    request.state.user_role = auth_session["role"]
    return auth_session


def require_admin(request: Request) -> dict:
    auth_session = require_user(request)
    if auth_session["role"] != "ADMIN":
        raise HTTPException(status_code=403, detail="Administrator access required.")
    return auth_session


def validate_csrf(request: Request, auth_session: dict) -> None:
    supplied = request.headers.get("X-CSRF-Token")
    if not supplied or not hmac.compare_digest(
        supplied,
        auth_session["csrf_token"],
    ):
        raise HTTPException(status_code=403, detail="CSRF validation failed.")


def client_key(request: Request, action: str) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    address = (
        forwarded.split(",", 1)[0].strip()
        if forwarded
        else (request.client.host if request.client else "unknown")
    )
    return f"{action}:{address}"


def enforce_rate_limit(
    request: Request,
    action: str,
    limit: int,
) -> None:
    allowed = request.app.state.chat_repository.check_rate_limit(
        client_key(request, action),
        limit,
    )
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="Too many attempts. Please try again later.",
        )


def cookie_settings(request: Request) -> str:
    secure = request.app.state.settings.environment in {"production", "prod"}
    expires = request.app.state.settings.session_expire_hours
    return f"Max-Age={expires * 60 * 60}; Path=/; HttpOnly; SameSite=Lax" + (
        "; Secure" if secure else ""
    )


@router.post("/register", response_model=AuthResponse, status_code=201)
def register(payload: RegisterRequest, request: Request, response: Response) -> AuthResponse:
    enforce_rate_limit(
        request,
        "register",
        request.app.state.settings.registration_rate_limit,
    )
    repository = request.app.state.chat_repository
    try:
        user = repository.register_user(
            str(payload.email),
            payload.password,
            payload.display_name,
            role="USER",
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    auth_session = repository.create_auth_session(
        user["id"],
        request.app.state.settings.session_expire_hours,
    )
    verification_token = repository.create_email_verification(user["id"], 60)
    email_service = request.app.state.email_service
    if email_service.is_configured():
        email_service.send(
            user["email"],
            "Verify your A&M FutureTech account",
            "Use this single-use verification token:\n\n" + verification_token,
        )
    repository.claim_legacy_session(
        getattr(request.state, "session_id", ""),
        user["id"],
    )
    response.set_cookie(
        AUTH_COOKIE_NAME,
        auth_session["session_id"],
        httponly=True,
        samesite=request.app.state.settings.cookie_samesite,
        secure=request.app.state.settings.cookie_secure or request.app.state.settings.environment in {"production", "prod"},
        max_age=request.app.state.settings.session_expire_hours * 60 * 60,
    )
    return AuthResponse(user=public_user(user), csrf_token=auth_session["csrf_token"])


@router.post("/login", response_model=AuthResponse)
def login(payload: LoginRequest, request: Request, response: Response) -> AuthResponse:
    enforce_rate_limit(
        request,
        "login",
        request.app.state.settings.login_rate_limit,
    )
    repository = request.app.state.chat_repository
    user = repository.authenticate_user(str(payload.email), payload.password)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    auth_session = repository.create_auth_session(
        user["id"],
        request.app.state.settings.session_expire_hours,
    )
    repository.claim_legacy_session(
        getattr(request.state, "session_id", ""),
        user["id"],
    )
    response.set_cookie(
        AUTH_COOKIE_NAME,
        auth_session["session_id"],
        httponly=True,
        samesite=request.app.state.settings.cookie_samesite,
        secure=request.app.state.settings.cookie_secure or request.app.state.settings.environment in {"production", "prod"},
        max_age=request.app.state.settings.session_expire_hours * 60 * 60,
    )
    return AuthResponse(user=public_user(user), csrf_token=auth_session["csrf_token"])


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response) -> None:
    auth_session = get_auth_session(request)
    if auth_session:
        validate_csrf(request, auth_session)
        request.app.state.chat_repository.revoke_auth_session(
            auth_session["session_id"]
        )
    response.delete_cookie(
        AUTH_COOKIE_NAME,
        path="/",
        samesite=request.app.state.settings.cookie_samesite,
        secure=request.app.state.settings.cookie_secure or request.app.state.settings.environment in {"production", "prod"},
    )


@router.get("/me", response_model=AuthResponse)
def me(request: Request) -> AuthResponse:
    auth_session = require_user(request)
    user = request.app.state.chat_repository.get_user_by_id(auth_session["user_id"])
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return AuthResponse(
        user=public_user(user),
        csrf_token=auth_session["csrf_token"],
    )


@router.post("/verification/resend", status_code=202)
def resend_verification(request: Request) -> dict:
    auth_session = require_user(request)
    validate_csrf(request, auth_session)
    enforce_rate_limit(
        request,
        "verification",
        3,
    )
    user = request.app.state.chat_repository.get_user_by_id(auth_session["user_id"])
    token = request.app.state.chat_repository.create_email_verification(
        auth_session["user_id"],
        60,
    )
    email_service = request.app.state.email_service
    if user and email_service.is_configured():
        email_service.send(
            user["email"],
            "Verify your A&M FutureTech account",
            "Use this single-use verification token:\n\n" + token,
        )
    return {"detail": "If email delivery is configured, verification instructions were sent."}


@router.post("/verify-email", status_code=204)
def verify_email(request: Request) -> None:
    token = request.headers.get("X-Verification-Token")
    if not token or not request.app.state.chat_repository.verify_email_token(token):
        raise HTTPException(
            status_code=400,
            detail="Verification token is invalid, expired, or already used.",
        )


@router.post("/change-password", status_code=204)
def change_password(
    payload: ChangePasswordRequest,
    request: Request,
) -> None:
    auth_session = require_user(request)
    validate_csrf(request, auth_session)
    changed = request.app.state.chat_repository.change_password(
        auth_session["user_id"],
        payload.current_password,
        payload.new_password,
    )
    if not changed:
        raise HTTPException(status_code=400, detail="Current password is incorrect.")


@router.post("/forgot-password", status_code=202)
def forgot_password(payload: ForgotPasswordRequest, request: Request) -> dict:
    enforce_rate_limit(
        request,
        "forgot",
        request.app.state.settings.reset_rate_limit,
    )
    token = request.app.state.chat_repository.create_password_reset(
        str(payload.email),
        request.app.state.settings.reset_token_minutes,
    )
    if token:
        email_service = request.app.state.email_service
        if email_service.is_configured():
            email_service.send(
                str(payload.email),
                "A&M FutureTech password reset",
                "Use this single-use reset token:\n\n" + token,
            )
    return {
        "detail": (
            "If an account exists for this email, password reset instructions "
            "may be sent when SMTP is configured."
        )
    }


@router.post("/reset-password", status_code=204)
def reset_password(payload: ResetPasswordRequest, request: Request) -> None:
    enforce_rate_limit(
        request,
        "reset",
        request.app.state.settings.reset_rate_limit,
    )
    reset = request.app.state.chat_repository.reset_password(
        payload.token,
        payload.new_password,
    )
    if not reset:
        raise HTTPException(
            status_code=400,
            detail="Reset token is invalid, expired, or already used.",
        )


@router.patch("/users/{user_id}/active", response_model=PublicUser)
def set_account_active(
    user_id: str,
    payload: ActivationRequest,
    request: Request,
) -> PublicUser:
    admin_session = require_admin(request)
    validate_csrf(request, admin_session)
    user = request.app.state.chat_repository.set_user_active(
        user_id,
        payload.active,
    )
    if user is None:
        raise HTTPException(status_code=404, detail="User not found.")
    return public_user(user)
