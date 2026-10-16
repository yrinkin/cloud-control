"""없는 테이블만 생성한다. 기존 테이블·데이터는 삭제하거나 변경하지 않는다.

실행: .venv/bin/python -m backend.init_db
"""

import sys

from sqlalchemy import inspect
from sqlalchemy.exc import SQLAlchemyError

# 모든 모델을 import해야 Base.metadata에 테이블이 등록된다.
from backend import models  # noqa: F401
from backend.config import ConfigError, load_db_settings
from backend.db import create_db_engine, describe_db_error
from backend.models import Base


def main() -> int:
    try:
        db = load_db_settings()
    except ConfigError as exc:
        print(f"[실패] 설정 오류: {exc}", file=sys.stderr)
        return 1

    engine = create_db_engine(db)
    try:
        with engine.connect() as conn:
            existing = set(inspect(conn).get_table_names())
        Base.metadata.create_all(engine, checkfirst=True)
        with engine.connect() as conn:
            after = set(inspect(conn).get_table_names())
    except SQLAlchemyError as exc:
        print(f"[실패] DB 오류 ({db.host}:{db.port}/{db.name}, 사용자 {db.user}): {describe_db_error(exc, db)}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()

    for table in Base.metadata.sorted_tables:
        if table.name in existing:
            print(f"[유지] {table.name}: 이미 있어 변경하지 않음")
        elif table.name in after:
            print(f"[생성] {table.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
