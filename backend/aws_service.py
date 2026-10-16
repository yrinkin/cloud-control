"""Boto3 조회·제어 client. AWS 예외를 AwsServiceError(status, code, message)로 바꾼다."""

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
