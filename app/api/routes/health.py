from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/")
def health_check(request: Request) -> dict[str, str]:
    settings = request.app.state.settings
    return {
        "status": "ok",
        "service": settings.app_name,
        "ai_mode": "demo" if settings.use_mock_ai else "live",
    }
