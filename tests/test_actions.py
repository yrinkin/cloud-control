"""EC2 제어 API 테스트. 실제 AWS·실습 DB·.env를 사용하지 않는다.

실행: .venv/bin/python -m unittest discover -s tests -v
"""

import os
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

TOKEN = "test-token"
ALLOWED = "i-0aaaaaaaaaaaaaaa1"
OTHER_ALLOWED = "i-0aaaaaaaaaaaaaaa2"
NOT_ALLOWED = "i-0bbbbbbbbbbbbbbbb"
ACCOUNT = "111122223333"

# 앱 import 전에 테스트 설정을 넣고 .env 경로를 바꿔 실제 .env를 읽지 않게 한다.
os.environ.update(
    AWS_PROFILE="test",
    AWS_REGION="ap-northeast-2",
    EXPECTED_ACCOUNT_ID=ACCOUNT,
    ADMIN_API_TOKEN=TOKEN,
    ALLOWED_INSTANCE_IDS=f"{ALLOWED}, {OTHER_ALLOWED}",
    DB_HOST="127.0.0.1",
    DB_PORT="1",
    DB_NAME="unused",
    DB_USER="unused",
    DB_PASSWORD="unused",
)

import backend.config as config  # noqa: E402

config.ENV_FILE = Path("/nonexistent/.env")

from botocore.exceptions import ClientError, EndpointConnectionError, ReadTimeoutError  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.exc import IntegrityError, OperationalError  # noqa: E402

import backend.main as main  # noqa: E402
from backend import ec2_control  # noqa: E402
from backend.aws_service import AwsServiceError  # noqa: E402
from backend.models import ActionLog  # noqa: E402

AUTH = {"Authorization": f"Bearer {TOKEN}"}


def db_error() -> OperationalError:
    return OperationalError("SELECT 1", {}, Exception("db down"))


class FakeStore:
    """ActionStore 대체. 메모리 dict에 저장하고, 지정한 메서드에서 DB 오류를 낸다."""

    def __init__(self):
        self.rows: dict[str, ActionLog] = {}
        self.fail_on: set[str] = set()
        self.integrity_on_create: ActionLog | None = None

    def _check(self, name):
        if name in self.fail_on:
            raise db_error()

    def get_by_request_id(self, request_id):
        self._check("get_by_request_id")
        return next((r for r in self.rows.values() if r.request_id == request_id), None)

    def has_unresolved(self, instance_id):
        self._check("has_unresolved")
        return any(r.instance_id == instance_id and r.status in ec2_control.UNRESOLVED for r in self.rows.values())

    def create_requested(self, request_id, instance_id, action):
        self._check("create_requested")
        if self.integrity_on_create is not None:
            # 다른 경로가 같은 request_id를 먼저 기록한 상황
            row = self.integrity_on_create
            self.rows[row.id] = row
            raise IntegrityError("INSERT", {}, Exception("Duplicate entry"))
        row = ActionLog(
            id=str(uuid.uuid4()), request_id=request_id, instance_id=instance_id, action=action,
            status=ec2_control.REQUESTED,
        )
        self.rows[row.id] = row
        return row

    def set_result(self, action_id, status, aws_request_id, message):
        self._check("set_result")
        row = self.rows[action_id]
        row.status, row.aws_request_id, row.message = status, aws_request_id, message

    def rollback(self):
        pass

    def add(self, instance_id, action, status):
        row = ActionLog(
            id=str(uuid.uuid4()), request_id=str(uuid.uuid4()), instance_id=instance_id, action=action, status=status
        )
        self.rows[row.id] = row
        return row


class FakeAws:
    """AwsService 대체. 변경 client는 Mock이며 호출 시 상태를 전환 상태로 바꾼다."""

    def __init__(self):
        self.states = {ALLOWED: "stopped", OTHER_ALLOWED: "running"}
        self.account = ACCOUNT
        self.change_delay = 0.0
        self.client = mock.Mock(name="ec2_control_client")
        self.client.start_instances.side_effect = self._start
        self.client.stop_instances.side_effect = self._stop
        self.get_context_calls = 0

    def _resp(self, key, iid, prev, curr):
        time.sleep(self.change_delay)
        self.states[iid] = curr
        return {
            key: [{"InstanceId": iid, "PreviousState": {"Name": prev}, "CurrentState": {"Name": curr}}],
            "ResponseMetadata": {"RequestId": "aws-req-1"},
        }

    def _start(self, InstanceIds):
        return self._resp("StartingInstances", InstanceIds[0], self.states[InstanceIds[0]], "pending")

    def _stop(self, InstanceIds):
        return self._resp("StoppingInstances", InstanceIds[0], self.states[InstanceIds[0]], "stopping")

    @property
    def change_calls(self) -> int:
        return self.client.start_instances.call_count + self.client.stop_instances.call_count

    # AwsService와 같은 인터페이스
    def __call__(self, settings):
        return self

    def get_context(self):
        self.get_context_calls += 1
        if self.account != ACCOUNT:
            raise AwsServiceError(403, "AWS_ACCOUNT_MISMATCH", "계정 불일치")
        return {"account_id": self.account, "caller_arn": "arn:aws:iam::x:user/s", "region": "ap-northeast-2"}

    def get_instance_state(self, instance_id):
        return self.states[instance_id]

    def control_client(self):
        return self.client


class ActionApiTest(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        self.aws = FakeAws()
        main.app.dependency_overrides[main.get_action_store] = lambda: self.store
        patcher = mock.patch.object(main, "AwsService", self.aws)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(main.app.dependency_overrides.clear)
        self.client = TestClient(main.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def post(self, instance_id=ALLOWED, action="start", request_id=None, headers=AUTH, **extra):
        body = {"action": action, "request_id": request_id or str(uuid.uuid4()), **extra}
        return self.client.post(f"/api/ec2/{instance_id}/actions", json=body, headers=headers)

    def assert_error(self, resp, status, code):
        self.assertEqual(resp.status_code, status, resp.text)
        self.assertEqual(resp.json()["detail"]["code"], code)
        self.assertIn("message", resp.json()["detail"])

    # ---- 인증·입력 검증 ----

    def test_auth_failure_makes_no_change_call(self):
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic x"}, {"Authorization": "Bearer "}):
            with self.subTest(headers=headers):
                self.assert_error(self.post(headers=headers), 401, "APP_AUTH_REQUIRED")
        self.assertEqual(self.aws.change_calls, 0)
        self.assertEqual(self.store.rows, {})

    def test_invalid_body_is_422_without_change_call(self):
        cases = [
            {"request_id": "not-a-uuid"},
            {"action": "terminate"},
            {"extra_field": "x"},
            {"region": "us-east-1"},
        ]
        for extra in cases:
            with self.subTest(extra=extra):
                body = {"action": "start", "request_id": str(uuid.uuid4()), **extra}
                resp = self.client.post(f"/api/ec2/{ALLOWED}/actions", json=body, headers=AUTH)
                self.assertEqual(resp.status_code, 422, resp.text)
        resp = self.client.post(f"/api/ec2/{ALLOWED}/actions", json={"action": "start"}, headers=AUTH)
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(self.aws.change_calls, 0)

    # ---- 허용 목록·계정·상태 ----

    def test_not_allowed_instance_is_403_before_any_aws_call(self):
        self.assert_error(self.post(instance_id=NOT_ALLOWED), 403, "INSTANCE_NOT_ALLOWED")
        self.assertEqual(self.aws.change_calls, 0)
        self.assertEqual(self.aws.get_context_calls, 0)
        self.assertEqual(self.store.rows, {})

    def test_empty_allowlist_rejects_all_control(self):
        empty = main.app.state.settings.__class__(
            **{**main.app.state.settings.__dict__, "allowed_instance_ids": frozenset()}
        )
        with mock.patch.object(main.app.state, "settings", empty):
            self.assert_error(self.post(), 403, "INSTANCE_NOT_ALLOWED")
        self.assertEqual(self.aws.change_calls, 0)

    def test_account_mismatch_is_403(self):
        self.aws.account = "999999999999"
        self.assert_error(self.post(), 403, "AWS_ACCOUNT_MISMATCH")
        self.assertEqual(self.aws.change_calls, 0)
        self.assertEqual(self.store.rows, {})

    def test_state_mismatch_is_409(self):
        for state, action in (("running", "start"), ("stopped", "stop"), ("pending", "start"), ("stopping", "stop")):
            with self.subTest(state=state, action=action):
                self.aws.states[ALLOWED] = state
                self.assert_error(self.post(action=action), 409, "INVALID_STATE")
        self.assertEqual(self.aws.change_calls, 0)
        self.assertEqual(self.store.rows, {})

    # ---- DB 실패 ----

    def test_db_failure_makes_no_change_call(self):
        for method in ("get_by_request_id", "has_unresolved", "create_requested"):
            with self.subTest(method=method):
                self.store.fail_on = {method}
                self.assert_error(self.post(), 503, "DB_UNAVAILABLE")
        self.assertEqual(self.aws.change_calls, 0)

    def test_db_update_failure_after_accept_keeps_requested_and_does_not_retry(self):
        self.store.fail_on = {"set_result"}
        rid = str(uuid.uuid4())
        self.assert_error(self.post(request_id=rid), 503, "DB_UPDATE_FAILED")
        self.assertEqual(self.aws.change_calls, 1)
        (row,) = self.store.rows.values()
        self.assertEqual(row.status, "REQUESTED")

        self.store.fail_on = set()
        resp = self.post(request_id=rid)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "REQUESTED")
        self.assert_error(self.post(), 409, "ACTION_IN_PROGRESS")
        self.assertEqual(self.aws.change_calls, 1)

    # ---- 멱등성 ----

    def test_first_accept_is_202_and_resend_is_200_with_single_change_call(self):
        rid = str(uuid.uuid4())
        first = self.post(request_id=rid)
        self.assertEqual(first.status_code, 202, first.text)
        body = first.json()
        self.assertEqual(body["status"], "ACCEPTED")
        self.assertEqual(body["request_id"], rid)

        for _ in range(3):
            again = self.post(request_id=rid)
            self.assertEqual(again.status_code, 200)
            self.assertEqual(again.json(), {"action_id": body["action_id"], "request_id": rid, "status": "ACCEPTED"})
        self.assertEqual(self.aws.change_calls, 1)
        self.assertEqual(len(self.store.rows), 1)

    def test_resend_is_checked_before_ec2_state(self):
        rid = str(uuid.uuid4())
        self.assertEqual(self.post(request_id=rid).status_code, 202)
        # 이제 인스턴스는 pending이지만 같은 키 재전송은 상태 409가 아니라 기존 결과다.
        self.assertEqual(self.aws.states[ALLOWED], "pending")
        self.assertEqual(self.post(request_id=rid).status_code, 200)
        self.assertEqual(self.aws.change_calls, 1)

    def test_same_key_different_content_is_409(self):
        rid = str(uuid.uuid4())
        self.assertEqual(self.post(request_id=rid).status_code, 202)
        self.assert_error(self.post(request_id=rid, action="stop"), 409, "IDEMPOTENCY_CONFLICT")
        self.assert_error(self.post(request_id=rid, instance_id=OTHER_ALLOWED), 409, "IDEMPOTENCY_CONFLICT")
        self.assertEqual(self.aws.change_calls, 1)

    def test_unique_conflict_on_insert_returns_existing_without_change_call(self):
        rid = str(uuid.uuid4())
        self.store.integrity_on_create = ActionLog(
            id=str(uuid.uuid4()), request_id=rid, instance_id=ALLOWED, action="start", status="ACCEPTED"
        )
        resp = self.post(request_id=rid)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "ACCEPTED")
        self.assertEqual(self.aws.change_calls, 0)

    # ---- 미확정 이력 ----

    def test_existing_requested_or_unknown_blocks_new_request(self):
        for status in ("REQUESTED", "UNKNOWN"):
            with self.subTest(status=status):
                self.store.rows.clear()
                self.store.add(ALLOWED, "start", status)
                self.assert_error(self.post(), 409, "ACTION_IN_PROGRESS")
        self.assertEqual(self.aws.change_calls, 0)

    def test_finished_history_does_not_block(self):
        self.store.add(ALLOWED, "start", "ACCEPTED")
        self.store.add(ALLOWED, "start", "FAILED")
        self.assertEqual(self.post().status_code, 202)

    # ---- AWS 결과 분류 ----

    def test_timeout_records_unknown_and_never_calls_again(self):
        self.aws.client.start_instances.side_effect = ReadTimeoutError(endpoint_url="https://ec2")
        rid = str(uuid.uuid4())
        self.assert_error(self.post(request_id=rid), 503, "AWS_RESULT_UNKNOWN")
        (row,) = self.store.rows.values()
        self.assertEqual(row.status, "UNKNOWN")
        self.assertEqual(self.aws.change_calls, 1)

        # 같은 키 재전송: 기존 UNKNOWN 반환, 추가 호출 없음
        resp = self.post(request_id=rid)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "UNKNOWN")
        # 새 키: 미확정 이력 때문에 차단, 추가 호출 없음
        self.assert_error(self.post(), 409, "ACTION_IN_PROGRESS")
        self.assertEqual(self.aws.change_calls, 1)

    def test_explicit_aws_rejection_records_failed(self):
        self.aws.client.start_instances.side_effect = ClientError(
            {"Error": {"Code": "UnauthorizedOperation", "Message": "no"}, "ResponseMetadata": {"RequestId": "r-9"}},
            "StartInstances",
        )
        self.assert_error(self.post(), 403, "AWS_ACCESS_DENIED")
        (row,) = self.store.rows.values()
        self.assertEqual((row.status, row.aws_request_id), ("FAILED", "r-9"))
        self.assertEqual(self.aws.change_calls, 1)
        # FAILED는 확정 결과이므로 다음 요청을 막지 않는다.
        self.aws.client.start_instances.side_effect = self.aws._start
        self.assertEqual(self.post().status_code, 202)

    def test_connection_failure_records_failed(self):
        self.aws.client.start_instances.side_effect = EndpointConnectionError(endpoint_url="https://ec2")
        self.assert_error(self.post(), 503, "AWS_CONNECTION_ERROR")
        (row,) = self.store.rows.values()
        self.assertEqual(row.status, "FAILED")

    def test_accept_records_aws_request_id(self):
        self.assertEqual(self.post(instance_id=OTHER_ALLOWED, action="stop").status_code, 202)
        (row,) = self.store.rows.values()
        self.assertEqual((row.status, row.aws_request_id, row.action), ("ACCEPTED", "aws-req-1", "stop"))
        self.aws.client.stop_instances.assert_called_once_with(InstanceIds=[OTHER_ALLOWED])

    # ---- lock ----

    def test_lock_is_created_once_and_shared(self):
        lock = ec2_control.CONTROL_LOCK
        self.assertIsInstance(lock, type(threading.Lock()))
        # 요청 처리 함수가 Lock을 새로 만들지 않는다(이름을 참조하지 않는다).
        self.assertNotIn("Lock", ec2_control.request_action.__code__.co_names)
        self.assertEqual(self.post().status_code, 202)
        self.assertEqual(self.post(instance_id=OTHER_ALLOWED, action="stop").status_code, 202)
        self.assertIs(ec2_control.CONTROL_LOCK, lock)
        self.assertFalse(lock.locked())

    def test_concurrent_requests_on_same_target_call_aws_once(self):
        self.aws.change_delay = 0.2
        results = []

        def worker():
            results.append(self.post().status_code)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results), [202, 409, 409, 409])
        self.assertEqual(self.aws.change_calls, 1)


if __name__ == "__main__":
    unittest.main()
