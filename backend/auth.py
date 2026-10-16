"""앱 고정 토큰 인증. 모든 /api 경로에 공통 의존성으로 적용한다."""

import secrets

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

bearer_scheme = HTTPBearer(auto_error=False)

AUTH_REQUIRED = HTTPException(
    status_code=401,
    detail={"code": "APP_AUTH_REQUIRED", "message": "앱 토큰이 없거나 올바르지 않습니다. 다시 연결하세요."},
    headers={"WWW-Authenticate": "Bearer"},
)


def require_app_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> None:
    # auto_error=False이면 헤더 누락·Bearer가 아닌 scheme·빈 값 모두 None으로 들어온다.
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AUTH_REQUIRED

    supplied = credentials.credentials.strip()
    if not supplied:
        raise AUTH_REQUIRED

    expected = request.app.state.settings.admin_api_token
    # 비ASCII 문자열은 compare_digest에서 TypeError가 나므로 bytes로 비교한다.
    if not secrets.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
        raise AUTH_REQUIRED
