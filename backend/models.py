"""DB 모델. 시각은 타임존 없는 UTC DATETIME으로 저장한다."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, String
from sqlalchemy.dialects.mysql import DATETIME as MYSQL_DATETIME
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

TABLE_ARGS = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}
# MariaDB에서는 DATETIME(6)으로 마이크로초까지 저장해 같은 초 안의 이력도 순서가 유지되게 한다.
UtcDateTime = DateTime().with_variant(MYSQL_DATETIME(fsp=6), "mysql", "mariadb")


def new_uuid() -> str:
    return str(uuid.uuid4())


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class ActionLog(Base):
    """EC2 시작·중지 요청 이력. AWS 호출 전에 REQUESTED로 기록하고 결과에 따라 status를 갱신한다."""

    __tablename__ = "action_logs"
    __table_args__ = TABLE_ARGS

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    request_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True, default=new_uuid)
    instance_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(16), nullable=False)  # start / stop
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # REQUESTED / ACCEPTED / FAILED / UNKNOWN
    aws_request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utc_now, index=True)
    message: Mapped[str | None] = mapped_column(String(500), nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "request_id": self.request_id,
            "instance_id": self.instance_id,
            "action": self.action,
            "status": self.status,
            "aws_request_id": self.aws_request_id,
            # 저장값은 UTC이므로 응답에는 +00:00을 붙여 브라우저가 현지 시각으로 바꿀 수 있게 한다.
            "created_at": self.created_at.replace(tzinfo=timezone.utc).isoformat(),
            "message": self.message,
        }
