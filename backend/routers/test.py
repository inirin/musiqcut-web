"""테스트 엔드포인트"""
from fastapi import APIRouter

router = APIRouter()


@router.get("/ping")
async def ping():
    """API 서버 상태 확인"""
    return {"ok": True, "message": "서버 정상 동작 중"}
