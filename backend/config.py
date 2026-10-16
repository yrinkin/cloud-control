"""앱 런타임 설정. 서버나 init_db가 load_*()를 호출할 때만 프로젝트 루트 .env를 읽는다."""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT_DIR / ".env"
FRONTEND_DIR = ROOT_DIR / "frontend"

ALLOWED_REGION = "ap-northeast-2"
APP_REQUIRED_VARS = ("AWS_PROFILE", "AWS_REGION", "EXPECTED_ACCOUNT_ID", "ADMIN_API_TOKEN")
DB_REQUIRED_VARS = ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD")


class ConfigError(Exception):
    """설정 오류. 메시지에는 변수 이름만 담고 값은 담지 않는다."""


@dataclass(frozen=True)
class DbSettings:
    host: str
    port: int
    name: str
    user: str
    # repr=False: 예외·로그에 설정 객체가 찍혀도 암호가 나오지 않게 한다.
    password: str = field(repr=False)


@dataclass(frozen=True)
class Settings:
    aws_profile: str
    aws_region: str
    expected_account_id: str
    admin_api_token: str = field(repr=False)
    db: DbSettings = field(repr=False)
    allowed_instance_ids: frozenset[str] = frozenset()


def parse_instance_ids(raw: str) -> frozenset[str]:
    """쉼표로 나누고 앞뒤 공백·빈 항목을 제거한 집합."""
    return frozenset(item.strip() for item in raw.split(",") if item.strip())


def _read_required(names: tuple[str, ...]) -> dict[str, str]:
    # override=False: 셸에서 이미 지정한 값이 있으면 그 값을 우선한다.
    load_dotenv(ENV_FILE, override=False)
    values = {name: (os.getenv(name) or "").strip() for name in names}

    missing = [name for name, value in values.items() if not value]
    if missing:
        raise ConfigError(f"필수 설정이 없습니다: {', '.join(missing)} (.env.example 참고)")

    # 예: ADMIN_API_TOKEN=ADMIN_API_TOKEN=abc 처럼 이름이 값에 중복으로 들어간 경우
    duplicated = [name for name, value in values.items() if value.upper().startswith(f"{name}=")]
    if duplicated:
        raise ConfigError(f"값에 변수 이름이 중복으로 들어 있습니다: {', '.join(duplicated)}")

    return values


def load_db_settings() -> DbSettings:
    values = _read_required(DB_REQUIRED_VARS)

    if not values["DB_PORT"].isdigit() or not 0 < int(values["DB_PORT"]) < 65536:
        raise ConfigError("DB_PORT는 1~65535 사이의 숫자여야 합니다.")

    return DbSettings(
        host=values["DB_HOST"],
        port=int(values["DB_PORT"]),
        name=values["DB_NAME"],
        user=values["DB_USER"],
        password=values["DB_PASSWORD"],
    )


def load_settings() -> Settings:
    values = _read_required(APP_REQUIRED_VARS)

    if values["AWS_REGION"] != ALLOWED_REGION:
        raise ConfigError(f"AWS_REGION은 {ALLOWED_REGION}만 허용합니다.")

    if not re.fullmatch(r"\d{12}", values["EXPECTED_ACCOUNT_ID"]):
        raise ConfigError("EXPECTED_ACCOUNT_ID는 12자리 숫자여야 합니다.")

    return Settings(
        aws_profile=values["AWS_PROFILE"],
        aws_region=values["AWS_REGION"],
        expected_account_id=values["EXPECTED_ACCOUNT_ID"],
        admin_api_token=values["ADMIN_API_TOKEN"],
        db=load_db_settings(),
        allowed_instance_ids=parse_instance_ids(os.getenv("ALLOWED_INSTANCE_IDS") or ""),
    )
