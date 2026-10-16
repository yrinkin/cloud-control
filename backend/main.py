"""FastAPI 진입점. 실행: uvicorn backend.main:app --host 127.0.0.1 --port 8001"""

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.aws_service import AwsService, AwsServiceError
from backend.config import FRONTEND_DIR, ConfigError, Settings, load_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 서버 기동 시점에만 .env를 읽는다. 누락 시 변수 이름만 담긴 ConfigError로 기동을 멈춘다.
    app.state.settings = load_settings()
    yield


app = FastAPI(title="Cloud Control", lifespan=lifespan)


def error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": {"code": code, "message": message}})


@app.exception_handler(AwsServiceError)
def handle_aws_error(request: Request, exc: AwsServiceError) -> JSONResponse:
    return error_response(exc.status_code, exc.code, exc.message)


@app.exception_handler(ConfigError)
def handle_config_error(request: Request, exc: ConfigError) -> JSONResponse:
    return error_response(500, "CONFIG_ERROR", str(exc))


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_verified_service(settings: Settings = Depends(get_settings)) -> AwsService:
    """STS로 계정을 확인한 뒤 서비스 객체를 돌려준다. 계정이 다르면 403."""
    service = AwsService(settings)
    service.get_context()
    return service


# Boto3는 동기 호출이므로 일반 def로 선언해 FastAPI 스레드풀에서 실행되게 한다.
@app.get("/api/context")
def read_context(settings: Settings = Depends(get_settings)) -> dict:
    return AwsService(settings).get_context()


@app.get("/api/ec2")
def read_ec2(service: AwsService = Depends(get_verified_service)) -> dict:
    items = service.list_ec2_instances()
    return {"items": items, "count": len(items)}


@app.get("/api/s3")
def read_s3(service: AwsService = Depends(get_verified_service)) -> dict:
    items = service.list_s3_buckets()
    return {"items": items, "count": len(items)}


@app.get("/api/security-groups")
def read_security_groups(service: AwsService = Depends(get_verified_service)) -> dict:
    items = service.list_security_groups()
    return {"items": items, "count": len(items)}


# API 라우트 뒤에 마운트한다. 루트 전체가 아니라 frontend 폴더만 공개한다.
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
