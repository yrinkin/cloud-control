"""Boto3 조회·제어 client. AWS 예외를 AwsServiceError(status, code, message)로 바꾼다."""

from datetime import datetime, timezone

import boto3
from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectionClosedError,
    ConnectionError,
    CredentialRetrievalError,
    LoginError,
    LoginInsufficientPermissions,
    NoCredentialsError,
    PartialCredentialsError,
    ProfileNotFound,
    ReadTimeoutError,
    SSOError,
    TokenRetrievalError,
)

from backend.config import Settings
from backend.security_check import inspect_group

# 점검 대상: 이 상태의 EC2에 실제로 연결된 보안 그룹만 본다. 제어 허용 목록과는 무관하다.
SCAN_INSTANCE_STATES = ["pending", "running", "stopping", "stopped"]
SG_BATCH_SIZE = 100
GROUP_NOT_FOUND_CODES = {"InvalidGroup.NotFound", "InvalidGroupId.NotFound"}

BOTO_CONFIG = Config(
    connect_timeout=5,
    read_timeout=15,
    retries={"max_attempts": 2, "mode": "standard"},
)
# 변경(시작·중지)용: 자동 재시도 없이 한 번만 보낸다. 재전송 판단은 request_id 이력으로 한다.
CONTROL_CONFIG = Config(
    connect_timeout=5,
    read_timeout=10,
    retries={"total_max_attempts": 1, "mode": "standard"},
)

ACCESS_DENIED_CODES = {
    "AccessDenied",
    "AccessDeniedException",
    "UnauthorizedOperation",
    "AuthorizationError",
}
LOGIN_REQUIRED_CODES = {
    "ExpiredToken",
    "ExpiredTokenException",
    "RequestExpired",
    "InvalidClientTokenId",
    "UnrecognizedClientException",
    "AuthFailure",
    "InvalidAccessKeyId",
    "SignatureDoesNotMatch",
}
LOGIN_ERRORS = (
    LoginError,
    NoCredentialsError,
    PartialCredentialsError,
    CredentialRetrievalError,
    TokenRetrievalError,
    SSOError,
    ProfileNotFound,
)
CONNECTION_ERRORS = (ConnectionError, ConnectionClosedError, ReadTimeoutError)


class AwsServiceError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


ACCESS_DENIED = (403, "AWS_ACCESS_DENIED", "이 작업에 필요한 AWS 권한이 없습니다.")
LOGIN_REQUIRED = (
    503,
    "AWS_LOGIN_REQUIRED",
    "AWS 로그인이 필요하거나 만료되었습니다. 터미널에서 aws login --profile <프로필>을 실행하세요.",
)
CONNECTION_ERROR = (503, "AWS_CONNECTION_ERROR", "AWS와 통신하지 못했습니다. 네트워크를 확인하고 다시 시도하세요.")


def translate_aws_error(exc: Exception) -> AwsServiceError:
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        if code.startswith("InvalidInstanceID"):
            return AwsServiceError(404, "INSTANCE_NOT_FOUND", "해당 EC2 인스턴스를 찾을 수 없습니다.")
        if code == "IncorrectInstanceState":
            return AwsServiceError(409, "INVALID_STATE", "현재 인스턴스 상태에서는 이 작업을 할 수 없습니다.")
        if code in ACCESS_DENIED_CODES:
            return AwsServiceError(*ACCESS_DENIED)
        if code in LOGIN_REQUIRED_CODES:
            return AwsServiceError(*LOGIN_REQUIRED)
        return AwsServiceError(502, "AWS_API_ERROR", f"AWS 요청이 실패했습니다. ({code or 'Unknown'})")
    if isinstance(exc, LoginInsufficientPermissions):
        return AwsServiceError(*ACCESS_DENIED)
    if isinstance(exc, LOGIN_ERRORS):
        return AwsServiceError(*LOGIN_REQUIRED)
    if isinstance(exc, CONNECTION_ERRORS):
        return AwsServiceError(*CONNECTION_ERROR)
    return AwsServiceError(502, "AWS_API_ERROR", "AWS 요청 처리 중 오류가 발생했습니다.")


def is_controllable(settings: Settings, instance_id: str) -> bool:
    """허용 목록은 서버 설정에서만 읽는다. 목록이 비어 있으면 어떤 인스턴스도 제어할 수 없다."""
    return instance_id in settings.allowed_instance_ids


def ensure_controllable(settings: Settings, instance_id: str) -> None:
    """시작·중지 전에 호출한다. 허용 목록 밖이면 AWS를 호출하기 전에 403으로 차단한다."""
    if not isinstance(instance_id, str) or not is_controllable(settings, instance_id):
        raise AwsServiceError(
            403,
            "INSTANCE_NOT_ALLOWED",
            "이 인스턴스는 서버의 제어 허용 목록(ALLOWED_INSTANCE_IDS)에 없습니다.",
        )


class AwsService:
    """요청마다 새로 만든다. 재로그인한 자격증명을 서버 재시작 없이 반영하기 위함."""

    def __init__(self, settings: Settings):
        self.settings = settings
        try:
            self.session = boto3.Session(
                profile_name=settings.aws_profile,
                region_name=settings.aws_region,
            )
        except (BotoCoreError, ClientError) as exc:
            raise translate_aws_error(exc) from exc

    def _client(self, name: str):
        return self.session.client(name, config=BOTO_CONFIG)

    def _call(self, fn):
        try:
            return fn()
        except (BotoCoreError, ClientError) as exc:
            raise translate_aws_error(exc) from exc

    def control_client(self):
        """시작·중지 전용 EC2 client. 자동 재시도하지 않는다."""
        try:
            return self.session.client("ec2", config=CONTROL_CONFIG)
        except (BotoCoreError, ClientError) as exc:
            raise translate_aws_error(exc) from exc

    def get_instance_state(self, instance_id: str) -> str:
        def run():
            resp = self._client("ec2").describe_instances(InstanceIds=[instance_id])
            for reservation in resp.get("Reservations", []):
                for inst in reservation.get("Instances", []):
                    if inst.get("InstanceId") == instance_id:
                        return inst.get("State", {}).get("Name", "unknown")
            raise AwsServiceError(404, "INSTANCE_NOT_FOUND", "해당 EC2 인스턴스를 찾을 수 없습니다.")

        return self._call(run)

    def get_context(self) -> dict:
        identity = self._call(lambda: self._client("sts").get_caller_identity())
        account_id = identity["Account"]
        if account_id != self.settings.expected_account_id:
            raise AwsServiceError(
                403,
                "AWS_ACCOUNT_MISMATCH",
                "로그인한 AWS 계정이 EXPECTED_ACCOUNT_ID와 다릅니다. 프로필과 .env 설정을 확인하세요.",
            )
        return {
            "account_id": account_id,
            "caller_arn": identity["Arn"],
            "region": self.settings.aws_region,
        }

    def list_ec2_instances(self) -> list[dict]:
        def run():
            items = []
            paginator = self._client("ec2").get_paginator("describe_instances")
            for page in paginator.paginate():
                for reservation in page.get("Reservations", []):
                    for inst in reservation.get("Instances", []):
                        tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
                        items.append(
                            {
                                "instance_id": inst["InstanceId"],
                                "name": tags.get("Name") or inst["InstanceId"],
                                "state": inst.get("State", {}).get("Name"),
                                "instance_type": inst.get("InstanceType"),
                                "private_ip": inst.get("PrivateIpAddress"),
                                "public_ip": inst.get("PublicIpAddress"),
                                "controllable": is_controllable(self.settings, inst["InstanceId"]),
                            }
                        )
            return items

        return self._call(run)

    def list_s3_buckets(self) -> list[dict]:
        def run():
            paginator = self._client("s3").get_paginator("list_buckets")
            return [
                {"name": bucket["Name"]}
                for page in paginator.paginate()
                for bucket in page.get("Buckets", [])
            ]

        return self._call(run)

    def list_security_groups(self) -> list[dict]:
        def run():
            paginator = self._client("ec2").get_paginator("describe_security_groups")
            return [
                {
                    "GroupId": sg["GroupId"],
                    "GroupName": sg["GroupName"],
                    "IpPermissions": sg.get("IpPermissions", []),
                }
                for page in paginator.paginate()
                for sg in page.get("SecurityGroups", [])
            ]

        return self._call(run)

    def scan_attached_security_groups(self) -> dict:
        """실제 EC2의 모든 ENI에 연결된 보안 그룹만 조회해 inspect_group으로 판정한다.

        DescribeInstances, DescribeSecurityGroups 조회만 사용한다.
        일부라도 확인하지 못하면 503 AWS_SCAN_INCOMPLETE로 실패시키고 '발견 없음'으로 바꾸지 않는다.
        """
        self.get_context()  # EXPECTED_ACCOUNT_ID 검증 (불일치 시 403)
        ec2 = self._client("ec2")

        def incomplete(message: str) -> AwsServiceError:
            return AwsServiceError(503, "AWS_SCAN_INCOMPLETE", f"{message} 점검 결과를 확정할 수 없으니 다시 조회하세요.")

        def collect_attachments():
            attachments: dict[str, dict[tuple[str, str], dict]] = {}
            instance_ids: set[str] = set()
            paginator = ec2.get_paginator("describe_instances")
            pages = paginator.paginate(Filters=[{"Name": "instance-state-name", "Values": SCAN_INSTANCE_STATES}])
            for page in pages:
                for reservation in page.get("Reservations", []):
                    for inst in reservation.get("Instances", []):
                        instance_id = inst.get("InstanceId")
                        enis = inst.get("NetworkInterfaces")
                        # 이 상태의 인스턴스는 ENI가 최소 1개 있어야 한다. 없으면 응답 누락으로 본다.
                        if not instance_id or not enis:
                            raise incomplete("인스턴스의 네트워크 인터페이스 정보가 응답에 없습니다.")
                        instance_ids.add(instance_id)
                        tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
                        name = tags.get("Name") or instance_id
                        state = inst.get("State", {}).get("Name")
                        for eni in enis:
                            eni_id = eni.get("NetworkInterfaceId")
                            groups = eni.get("Groups")
                            if not eni_id or not groups:
                                raise incomplete("네트워크 인터페이스의 보안 그룹 정보가 응답에 없습니다.")
                            for group in groups:
                                group_id = group.get("GroupId")
                                if not group_id:
                                    raise incomplete("보안 그룹 ID가 응답에 없습니다.")
                                # (instance_id, network_interface_id, group_id) 기준으로 중복 제거
                                attachments.setdefault(group_id, {})[(instance_id, eni_id)] = {
                                    "instance_id": instance_id,
                                    "instance_name": name,
                                    "state": state,
                                    "network_interface_id": eni_id,
                                }
            return len(instance_ids), attachments

        def describe_groups(group_ids: list[str]) -> dict[str, dict]:
            found: dict[str, dict] = {}
            for i in range(0, len(group_ids), SG_BATCH_SIZE):
                batch = group_ids[i : i + SG_BATCH_SIZE]
                # 빈 GroupIds는 '전체 보안 그룹 조회'가 되므로 절대 보내지 않는다.
                if not batch:
                    raise incomplete("조회할 보안 그룹 ID 묶음이 비어 있습니다.")
                try:
                    resp = ec2.describe_security_groups(GroupIds=batch)
                except ClientError as exc:
                    if exc.response.get("Error", {}).get("Code") in GROUP_NOT_FOUND_CODES:
                        raise incomplete("조회 중 일부 보안 그룹을 찾을 수 없습니다(삭제 등).") from exc
                    raise
                returned = {sg["GroupId"]: sg for sg in resp.get("SecurityGroups", [])}
                if set(returned) != set(batch):
                    raise incomplete("요청한 보안 그룹과 반환된 보안 그룹이 일치하지 않습니다.")
                found.update(returned)
            return found

        def result(instance_count: int, checked_groups: list[dict], findings: list[dict]) -> dict:
            return {
                "findings": findings,
                "checked_groups": checked_groups,
                "checked_instance_count": instance_count,
                "checked_group_count": len({g["group_id"] for g in checked_groups}),
                "finding_count": len(findings),
                "checked_at": datetime.now(timezone.utc).isoformat(),
            }

        def run():
            instance_count, attachments = collect_attachments()
            group_ids = sorted(attachments)
            if not group_ids:
                # 대상 EC2에 연결된 그룹이 없다. 보안 그룹 조회를 호출하지 않는다.
                return result(instance_count, [], [])

            described = describe_groups(group_ids)
            checked_groups, findings = [], []
            for group_id in group_ids:
                connections = sorted(
                    attachments[group_id].values(), key=lambda a: (a["instance_id"], a["network_interface_id"])
                )
                # 원본 GroupId/GroupName/IpPermissions 구조 그대로, 그룹마다 한 번만 판정한다.
                # finding은 규칙·공개 CIDR 조합 단위이며 연결 인스턴스 수만큼 늘리지 않는다.
                for finding in inspect_group(described[group_id]):
                    findings.append({**finding, "connections": connections})
                checked_groups.append(
                    {
                        "group_id": group_id,
                        "group_name": described[group_id].get("GroupName", ""),
                        "connections": connections,
                    }
                )
            return result(instance_count, checked_groups, findings)

        return self._call(run)
