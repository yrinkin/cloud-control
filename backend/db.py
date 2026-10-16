"""DB 엔진·세션 생성. FastAPI(main.py)를 import하지 않고 config만 사용한다."""

from sqlalchemy import URL, Engine, create_engine
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from backend.config import DbSettings


def build_url(db: DbSettings) -> URL:
    return URL.create(
        drivername="mysql+pymysql",
        username=db.user,
        password=db.password,
        host=db.host,
        port=db.port,
        database=db.name,
        query={"charset": "utf8mb4"},
    )


def create_db_engine(db: DbSettings) -> Engine:
    # 엔진 생성만으로는 접속하지 않는다. 실제 접속은 첫 쿼리 때 일어난다.
    return create_engine(
        build_url(db),
        pool_pre_ping=True,
        pool_recycle=3600,
        hide_parameters=True,  # 오류 메시지에 SQL 파라미터 값을 넣지 않는다.
        connect_args={"connect_timeout": 5, "read_timeout": 10, "write_timeout": 10},
    )


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    # 세션은 요청마다 이 팩토리로 새로 열고 요청이 끝나면 닫는다.
    return sessionmaker(bind=engine, expire_on_commit=False)


def describe_db_error(exc: SQLAlchemyError, db: DbSettings) -> str:
    """비밀값을 제외한 DB 오류 원인. 드라이버 오류 코드와 메시지만 사용한다."""
    orig = exc.orig if isinstance(exc, DBAPIError) else None
    if orig is not None and getattr(orig, "args", None):
        args = orig.args
        reason = f"[{args[0]}] {args[1]}" if len(args) >= 2 else str(args[0])
    else:
        reason = type(exc).__name__
    # 드라이버 메시지에 암호가 들어갈 일은 없지만, 혹시 모를 경우를 대비해 가린다.
    if db.password:
        reason = reason.replace(db.password, "***")
    return reason
