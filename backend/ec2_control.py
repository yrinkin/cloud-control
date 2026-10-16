"""EC2 시작·중지. 학습용 단일 프로세스 앱 전제.

순서: 허용 ID 검사 → (lock) 중복 키 → 미확정 이력 → 예상 계정 → 최신 EC2 상태
      → REQUESTED commit → AWS 변경 호출 1회 → ACCEPTED / FAILED / UNKNOWN 기록.
terminate, 강제 중지, 자동 재시도, waiter, 상태 폴링은 하지 않는다.
"""

import threading
from collections.abc import Callable
from typing import Any

from botocore.exceptions import ClientError
from botocore.exceptions import ConnectionError as BotoConnectionError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from backend.aws_service import LOGIN_ERRORS, AwsService, AwsServiceError, ensure_controllable, translate_aws_error
from backend.config import Settings
from backend.models import ActionLog

# 프로세스에서 한 번만 만든다. 모든 변경 진입점이 이 lock을 공유한다.
CONTROL_LOCK = threading.Lock()

REQUESTED = "REQUESTED"
ACCEPTED = "ACCEPTED"
FAILED = "FAILED"
UNKNOWN = "UNKNOWN"
UNRESOLVED = (REQUESTED, UNKNOWN)

REQUIRED_STATE = {"start": "stopped", "stop": "running"}
MESSAGE_MAX = 500


class ActionError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


DB_UNAVAILABLE = (503, "DB_UNAVAILABLE", "DB에 기록하지 못해 요청을 처리하지 않았습니다. MariaDB 상태를 확인하세요.")


class ActionStore:
    """action_logs 저장 함수 모음. 테스트에서는 이 객체를 대체한다."""

    def __init__(self, session: Session):
        self.session = session

    def get_by_request_id(self, request_id: str) -> ActionLog | None:
        return self.session.scalar(select(ActionLog).where(ActionLog.request_id == request_id))

    def has_unresolved(self, instance_id: str) -> bool:
        stmt = select(ActionLog.id).where(
            ActionLog.instance_id == instance_id, ActionLog.status.in_(UNRESOLVED)
        ).limit(1)
        return self.session.scalar(stmt) is not None

    def create_requested(self, request_id: str, instance_id: str, action: str) -> ActionLog:
        log = ActionLog(request_id=request_id, instance_id=instance_id, action=action, status=REQUESTED)
        self.session.add(log)
        self.session.commit()
        return log

    def set_result(self, action_id: str, status: str, aws_request_id: str | None, message: str | None) -> None:
        log = self.session.get(ActionLog, action_id)
        log.status = status
        log.aws_request_id = aws_request_id
        log.message = (message or "")[:MESSAGE_MAX] or None
        self.session.commit()

    def rollback(self) -> None:
        self.session.rollback()


def _db(store: ActionStore, fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except IntegrityError:
        raise
    except SQLAlchemyError as exc:
        _safe_rollback(store)
        raise ActionError(*DB_UNAVAILABLE) from exc


def _safe_rollback(store: ActionStore) -> None:
    try:
        store.rollback()
    except Exception:  # noqa: BLE001 - rollback 실패는 원래 오류를 가리지 않게 무시한다.
        pass


def _replay(existing: ActionLog, instance_id: str, action: str) -> tuple[int, dict]:
    """같은 request_id 재전송. 내용이 같으면 기존 결과, 다르면 409. AWS는 다시 호출하지 않는다."""
    if existing.instance_id != instance_id or existing.action != action:
        raise ActionError(
            409, "IDEMPOTENCY_CONFLICT", "같은 request_id가 다른 대상 또는 작업으로 이미 사용되었습니다."
        )
    return 200, {"action_id": existing.id, "request_id": existing.request_id, "status": existing.status}


def _aws_request_id(source: dict | None) -> str | None:
    return (source or {}).get("ResponseMetadata", {}).get("RequestId")


def _call_change(client, instance_id: str, action: str) -> dict:
    if action == "start":
        return client.start_instances(InstanceIds=[instance_id])
    return client.stop_instances(InstanceIds=[instance_id])


def request_action(
    settings: Settings,
    store: ActionStore,
    service_factory: Callable[[Settings], AwsService],
    instance_id: str,
    action: str,
    request_id: str,
) -> tuple[int, dict]:
    # 1. 허용 목록 밖이면 DB·AWS를 건드리기 전에 403.
    ensure_controllable(settings, instance_id)

    # 2. lock 밖 빠른 확인: 재전송이면 lock을 기다리지 않고 기존 결과를 돌려준다.
    existing = _db(store, lambda: store.get_by_request_id(request_id))
    if existing is not None:
        return _replay(existing, instance_id, action)

    with CONTROL_LOCK:
        # 3. lock 안에서 다시 확인한다. 중복 판정은 EC2 상태 검사보다 먼저 한다.
        existing = _db(store, lambda: store.get_by_request_id(request_id))
        if existing is not None:
            return _replay(existing, instance_id, action)

        if _db(store, lambda: store.has_unresolved(instance_id)):
            raise ActionError(
                409,
                "ACTION_IN_PROGRESS",
                "이 인스턴스에 결과가 확정되지 않은 요청(REQUESTED 또는 UNKNOWN)이 있어 새 제어를 거절합니다.",
            )

        # 4. 예상 계정(STS)과 최신 EC2 상태. 불일치면 403 / 409.
        service = service_factory(settings)
        service.get_context()
        state = service.get_instance_state(instance_id)
        required = REQUIRED_STATE[action]
        if state != required:
            raise ActionError(
                409, "INVALID_STATE", f"{action}은(는) {required} 상태에서만 가능합니다. 현재 상태: {state}"
            )
        client = service.control_client()

        # 5. REQUESTED를 commit한 뒤에만 AWS를 호출한다. 실패하면 호출하지 않는다.
        try:
            log = _db(store, lambda: store.create_requested(request_id, instance_id, action))
        except IntegrityError:
            # request_id UNIQUE 충돌: 다른 경로에서 먼저 기록됐다. 그 기록을 기준으로 응답한다.
            _safe_rollback(store)
            existing = _db(store, lambda: store.get_by_request_id(request_id))
            if existing is None:
                raise ActionError(*DB_UNAVAILABLE) from None
            return _replay(existing, instance_id, action)
        action_id = log.id

        # 6. AWS 변경 호출은 정확히 한 번.
        error: ActionError | AwsServiceError | None = None
        try:
            resp = _call_change(client, instance_id, action)
            changes = resp.get("StartingInstances") or resp.get("StoppingInstances") or []
            transition = ""
            if changes:
                prev = changes[0].get("PreviousState", {}).get("Name")
                curr = changes[0].get("CurrentState", {}).get("Name")
                transition = f": {prev} → {curr}"
            status, aws_req, message = ACCEPTED, _aws_request_id(resp), f"AWS 접수{transition}"
        except ClientError as exc:
            # AWS가 명시적으로 거절했다.
            code = exc.response.get("Error", {}).get("Code", "Unknown")
            status, aws_req, message = FAILED, _aws_request_id(exc.response), f"AWS 거절: {code}"
            error = translate_aws_error(exc)
        except (BotoConnectionError, *LOGIN_ERRORS) as exc:
            # 연결 수립 실패·자격증명 오류: 요청이 AWS에 전달되지 않았다.
            status, aws_req, message = FAILED, None, f"요청 전송 실패: {type(exc).__name__}"
            error = translate_aws_error(exc)
        except Exception as exc:  # noqa: BLE001 - 읽기 timeout 등: AWS가 처리했는지 알 수 없다.
            status, aws_req, message = UNKNOWN, None, f"결과 불확실: {type(exc).__name__}"
            error = ActionError(
                503,
                "AWS_RESULT_UNKNOWN",
                "AWS가 요청을 처리했는지 확인하지 못했습니다. 같은 요청을 다시 보내지 말고 EC2 상태를 새로고침해 확인하세요.",
            )

        # 7. 결과 기록. 실패해도 REQUESTED를 지우거나 AWS를 다시 호출하지 않는다.
        try:
            _db(store, lambda: store.set_result(action_id, status, aws_req, message))
        except ActionError:
            raise ActionError(
                503,
                "DB_UPDATE_FAILED",
                f"AWS 호출 결과({status})를 이력에 기록하지 못했습니다. 이력은 REQUESTED로 남아 같은 대상의 새 제어가 차단됩니다.",
            ) from None

        if error is not None:
            raise error
        return 202, {"action_id": action_id, "request_id": request_id, "status": ACCEPTED}
