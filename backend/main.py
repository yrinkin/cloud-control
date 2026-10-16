"""FastAPI 진입점. 실행: uvicorn backend.main:app --host 127.0.0.1 --port 8001"""

import logging
from collections.abc import Iterator
from contextlib import asynccontextmanager
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.exc import DisconnectionError, InterfaceError, OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

from backend.auth import require_app_token
from backend.aws_service import AwsService, AwsServiceError
from backend.config import FRONTEND_DIR, ConfigError, Settings, load_settings
from backend.db import create_db_engine, create_session_factory
from backend.ec2_control import ActionError, ActionStore, request_action
from backend.models import ActionLog

logger = logging.getLogger("cloud_control")

ACTIONS_LIMIT = 50


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 서버 기동 시점에만 .env를 읽는다. 누락 시 변수 이름만 담긴 ConfigError로 기동을 멈춘다.
    settings = load_settings()
    app.state.settings = settings
    engine = create_db_engine(settings.db)
    app.state.session_factory = create_session_factory(engine)
    try:
        yield
    finally:
        engine.dispose()


app = FastAPI(title="Cloud Control", lifespan=lifespan)


def error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": {"code": code, "message": message}})


@app.exception_handler(AwsServiceError)
def handle_aws_error(request: Request, exc: AwsServiceError) -> JSONResponse:
    return error_response(exc.status_code, exc.code, exc.message)


@app.exception_handler(ActionError)
def handle_action_error(request: Request, exc: ActionError) -> JSONResponse:
    return error_response(exc.status_code, exc.code, exc.message)


@app.exception_handler(ConfigError)
def handle_config_error(request: Request, exc: ConfigError) -> JSONResponse:
    return error_response(500, "CONFIG_ERROR", str(exc))


@app.exception_handler(SQLAlchemyError)
def handle_db_error(request: Request, exc: SQLAlchemyError) -> JSONResponse:
    # 예외 메시지에는 SQL·접속 정보가 섞일 수 있어 로그에는 예외 종류만 남긴다.
    logger.warning("DB 오류: %s", type(exc).__name__)
    if isinstance(exc, (OperationalError, InterfaceError, DisconnectionError)):
        return error_response(503, "DB_UNAVAILABLE", "DB에 연결하지 못했습니다. MariaDB 상태와 DB 설정을 확인하세요.")
    return error_response(500, "DB_ERROR", "DB 처리 중 오류가 발생했습니다.")


def get_db(request: Request) -> Iterator[Session]:
    """요청마다 세션을 새로 열고 응답 후 닫는다. 여러 요청이 세션을 공유하지 않는다."""
    with request.app.state.session_factory() as session:
        yield session


def get_action_store(db: Session = Depends(get_db)) -> ActionStore:
    return ActionStore(db)


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_verified_service(settings: Settings = Depends(get_settings)) -> AwsService:
    """STS로 계정을 확인한 뒤 서비스 객체를 돌려준다. 계정이 다르면 403."""
    service = AwsService(settings)
    service.get_context()
    return service


# 모든 /api 경로는 이 라우터에 두어 앱 토큰 인증을 먼저 거친다.
# 계정·리전·허용 목록은 서버 설정에서만 읽고 요청 파라미터로 받지 않는다.
api = APIRouter(prefix="/api", dependencies=[Depends(require_app_token)])


# Boto3는 동기 호출이므로 일반 def로 선언해 FastAPI 스레드풀에서 실행되게 한다.
@api.get("/context")
def read_context(settings: Settings = Depends(get_settings)) -> dict:
    return AwsService(settings).get_context()


@api.get("/ec2")
def read_ec2(service: AwsService = Depends(get_verified_service)) -> dict:
    items = service.list_ec2_instances()
    return {"items": items, "count": len(items)}


@api.get("/s3")
def read_s3(service: AwsService = Depends(get_verified_service)) -> dict:
    items = service.list_s3_buckets()
    return {"items": items, "count": len(items)}


@api.get("/security-groups")
def read_security_groups(service: AwsService = Depends(get_verified_service)) -> dict:
    items = service.list_security_groups()
    return {"items": items, "count": len(items)}


class ActionRequest(BaseModel):
    # 추가 필드는 422로 거절한다. 대상 ID는 경로에서만, 계정·리전·허용 목록은 서버 설정에서만 정한다.
    model_config = ConfigDict(extra="forbid")

    action: Literal["start", "stop"]
    request_id: UUID


@api.post("/ec2/{instance_id}/actions")
def create_ec2_action(
    instance_id: str,
    body: ActionRequest,
    settings: Settings = Depends(get_settings),
    store: ActionStore = Depends(get_action_store),
) -> JSONResponse:
    # 동기 Boto3·DB 호출과 threading.Lock을 쓰므로 일반 def로 둔다.
    status_code, payload = request_action(
        settings, store, AwsService, instance_id, body.action, str(body.request_id)
    )
    return JSONResponse(status_code=status_code, content=payload)


@api.get("/security-checks")
def read_security_checks(settings: Settings = Depends(get_settings)) -> dict:
    # 요청마다 AWS에서 새로 조회한다. 결과를 캐시하거나 DB에 저장하지 않는다.
    return AwsService(settings).scan_attached_security_groups()


@api.get("/actions")
def read_actions(db: Session = Depends(get_db)) -> dict:
    rows = db.scalars(
        select(ActionLog).order_by(ActionLog.created_at.desc(), ActionLog.id.desc()).limit(ACTIONS_LIMIT)
    ).all()
    items = [row.to_dict() for row in rows]
    return {"items": items, "count": len(items)}


@api.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
def api_not_found(path: str) -> JSONResponse:
    # 없는 /api 경로도 인증을 먼저 요구하고, 정적 파일 마운트로 넘어가지 않게 한다.
    return error_response(404, "NOT_FOUND", "존재하지 않는 API입니다.")


app.include_router(api)

# API 라우트 뒤에 마운트한다. 루트 전체가 아니라 frontend 폴더만 공개하며 인증을 요구하지 않는다.
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
