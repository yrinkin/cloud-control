# Cloud Control

AWS 서울 리전(`ap-northeast-2`)의 리소스를 웹에서 다루는 **학습용** 앱입니다. 할 수 있는 일은 세 가지입니다.

1. **조회**: EC2, S3 버킷, 보안 그룹
2. **지정 EC2 제어**: 서버 허용 목록에 있는 EC2만 시작·중지
3. **보안 그룹 규칙 점검**: 실제 EC2에 연결된 보안 그룹에서 공개 관리 포트(TCP 22·3389) 규칙 찾기

## 사용 범위

- `127.0.0.1:8001`에서 **단일 프로세스**로 실행하는 **localhost 전용 학습용 앱**입니다.
  - EC2 제어의 중복 방지 lock은 한 프로세스 안에서만 유효합니다. `--workers`를 늘리거나 여러 서버를 띄우지 마세요.
  - `--host 0.0.0.0`으로 외부에 공개하지 마세요. 앱 토큰 하나로 EC2를 제어할 수 있습니다.
- AWS 인증은 학생 본인의 관리자 권한 IAM 사용자로 받은 임시 자격증명(`aws login --profile lab`)을 씁니다.

### 지원하지 않는 기능

| 구분 | 지원 여부 |
|---|---|
| EC2 **시작·중지** (허용 목록 대상만) | 지원 |
| 리소스 **생성·삭제** (EC2 생성·terminate, 버킷·보안 그룹 생성·삭제) | 지원 안 함 |
| 보안 그룹 규칙 **자동 수정·삭제·추가** | 지원 안 함. 점검 결과는 표시만 함 |
| **예약** 실행, 자동 폴링, 자동 재시도 | 지원 안 함. 상태는 새로고침 버튼으로 확인 |
| **외부 배포**, 여러 사용자, 사용자 관리 | 지원 안 함 |

## 실행 순서

### 1. WSL에서 프로젝트 폴더로 이동

프로젝트는 WSL Ubuntu 안에 있습니다.

```bash
cd ~/vibe-lab/cloud-control          # = /home/<사용자>/vibe-lab/cloud-control
```

Windows 탐색기에서는 `\\wsl$\<배포판 이름>\home\<사용자>\vibe-lab\cloud-control`로 열 수 있습니다.
배포판 이름은 cmd에서 `wsl -l -v`로 확인합니다.

### 2. 가상환경 (최초 1회 생성)

```bash
python3 -m venv .venv
.venv/bin/pip install fastapi uvicorn boto3 python-dotenv sqlalchemy pymysql httpx
source .venv/bin/activate            # 이후 명령에서 python, uvicorn을 바로 쓰려면
```

확인한 버전: Python 3.12, FastAPI 0.143, Boto3 1.43, SQLAlchemy 2.1, PyMySQL 1.2, MariaDB 10.11.
`httpx`는 테스트에만 필요합니다.

### 3. MariaDB 시작

```bash
sudo systemctl start mariadb         # systemd가 없는 WSL이면: sudo service mariadb start
systemctl is-active mariadb          # active
```

처음 한 번은 DB와 사용자를 만듭니다 (예시, 암호는 직접 정함).

```sql
CREATE DATABASE cloud_control CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;
CREATE USER 'cloud_control'@'localhost' IDENTIFIED BY '<직접 정한 암호>';
GRANT ALL PRIVILEGES ON cloud_control.* TO 'cloud_control'@'localhost';
```

### 4. `.env` 작성 (최초 1회)

`.env.example`을 `.env`로 복사해 실제 값을 넣습니다. `.env`는 `.gitignore`에 포함되어 있어 Git에 올라가지 않습니다.

| 변수 | 용도 |
|---|---|
| `AWS_PROFILE` | 사용할 AWS CLI 프로필 이름 (`lab`) |
| `AWS_REGION` | 리전. `ap-northeast-2`만 허용하며 다른 값이면 서버가 시작되지 않음 |
| `EXPECTED_ACCOUNT_ID` | 이 앱이 다룰 12자리 계정 ID. 로그인한 계정이 다르면 모든 AWS 요청을 차단 |
| `ADMIN_API_TOKEN` | 앱 토큰 (아래 "앱 토큰" 참고) |
| `ALLOWED_INSTANCE_IDS` | 시작·중지를 허용할 인스턴스 ID 목록, 쉼표로 구분 |
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | MariaDB 접속 정보 |

값은 따옴표 없이 `이름=값` 형태로 한 번만 적습니다.
필수 값이 비어 있거나 `ADMIN_API_TOKEN=ADMIN_API_TOKEN=...`처럼 이름이 중복되면, 서버가 **변수 이름만** 알려 주고 시작을 거절합니다.

테이블은 최초 1회 만듭니다. 이 명령은 없는 테이블만 만들고 기존 데이터는 건드리지 않습니다.

```bash
.venv/bin/python -m backend.init_db
```

### 5. AWS 로그인

```bash
aws login --profile lab
```

임시 자격증명이라 시간이 지나면 만료됩니다. 화면에 `AWS_LOGIN_REQUIRED` 배너가 뜨면 이 명령을 다시 실행하세요. 서버 재시작은 필요 없습니다.

### 6. 서버 실행

```bash
.venv/bin/uvicorn backend.main:app --host 127.0.0.1 --port 8001
```

`.env`나 코드를 바꾼 뒤에는 `Ctrl+C`로 끄고 다시 실행해야 반영됩니다. 서버는 시작할 때만 `.env`를 읽습니다.

### 7. Windows 브라우저로 접속

Windows의 브라우저에서 http://127.0.0.1:8001 을 엽니다. WSL2는 localhost 요청을 WSL 안으로 전달합니다.
앱 토큰을 입력하고 **연결**을 누릅니다.

화면이 예전 모습 그대로라면 `Ctrl+Shift+R`로 강력 새로고침하세요.

터미널에서 API를 확인할 수도 있습니다. 토큰은 `.env`에서 읽고 출력하지 않습니다.

```bash
.venv/bin/python check_api.py /api/context
.venv/bin/python check_api.py /api/security-checks
```

## 앱 토큰

- `ADMIN_API_TOKEN`은 **실습용 고정 문자열**입니다. 로그인할 때마다 발급되는 JWT나 세션이 아니며, 만료 시각도 없습니다.
- 직접 만들어 `.env`에 넣습니다.

  ```bash
  python3 -c "import secrets; print(secrets.token_urlsafe(32))"
  ```

- 브라우저는 입력한 토큰을 JS 메모리에만 두고 모든 `/api` 요청에 `Authorization: Bearer <토큰>`으로 붙입니다.
  URL, localStorage, 로그에는 남기지 않으며, 새로고침이나 **로그아웃**을 하면 사라집니다.
- 토큰을 바꾸려면 `.env`를 고치고 서버를 재시작합니다. 아이디·비밀번호 로그인은 없습니다.
- 오류 두 가지를 구분합니다.

  | 오류 | 원인 | 화면 동작 |
  |---|---|---|
  | `401 APP_AUTH_REQUIRED` | 앱 토큰 문제 | 토큰 입력 화면으로 돌아감 |
  | `503 AWS_LOGIN_REQUIRED` | AWS 로그인 만료 | 토큰은 유지하고 `aws login` 안내 배너 표시 |

## 화면

| 탭 | 내용 |
|---|---|
| 대시보드 | 계정·리전·호출자 ARN, 요약 숫자 |
| EC2 | 인스턴스 목록, 상태 필터, 허용 대상 시작·중지 |
| S3 | 계정의 버킷 이름 |
| 보안 그룹 | 서울 리전 전체 보안 그룹의 인바운드 규칙 |
| 실행 이력 | 최근 50건의 제어 이력, 마지막 EC2 조회 기준 현재 상태 |
| 보안 점검 | '점검 실행'을 누를 때마다 AWS에서 새로 조회해 판정 |

- 요약 숫자는 실제 조회 결과로만 계산합니다. 조회에 실패하면 0이 아니라 **'확인 불가'**로 표시합니다.
- EC2 상태 필터는 이미 조회한 목록 안에서만 거릅니다. AWS를 다시 호출하지 않습니다.

## EC2 시작·중지

### 허용 목록 (`ALLOWED_INSTANCE_IDS`)

- 시작·중지할 수 있는 인스턴스를 정하는 **서버 설정**입니다. 화면에서는 바꿀 수 없습니다.
- 바꾸려면 `.env`를 편집하고 **서버를 재시작**합니다.
- 목록에서 ID를 빼도 **EC2는 삭제되지 않습니다.** 이 앱에서 제어할 수 없게 될 뿐이고, 조회와 보안 점검에는 계속 나옵니다.
- 목록이 비어 있으면 모든 시작·중지가 거절됩니다. 조회는 그대로 됩니다.
- 목록 밖 ID로 요청하면 AWS를 호출하기 전에 `403 INSTANCE_NOT_ALLOWED`로 거절합니다.

### 요청 흐름

1. 허용 대상이면서 `stopped`이면 **시작**, `running`이면 **중지** 버튼만 켜집니다.
2. 확인창에 계정, 리전, 이름, ID, 작업이 나옵니다. **승인**하면 요청 키(`request_id`, UUID)를 만들어 보내고, **취소**하면 아무것도 보내지 않습니다.
3. 서버는 다음을 순서대로 확인합니다.
   - 앱 토큰
   - 허용 목록
   - 같은 `request_id`의 재전송 여부
   - 결과가 확정되지 않은 이력이 있는지
   - 예상 계정
   - 현재 EC2 상태
4. 모두 통과하면 DB에 `REQUESTED`를 먼저 기록하고, 그 뒤 AWS를 **한 번만** 호출합니다. 자동 재시도는 하지 않습니다.
5. 같은 `request_id`로 다시 보내면 AWS를 다시 호출하지 않고 기존 기록을 돌려줍니다.

### 이력 상태

| 상태 | 의미 |
|---|---|
| `REQUESTED` | DB에 기록했지만 AWS 결과를 아직 반영하지 못함 |
| `ACCEPTED` | **AWS가 요청을 접수함. 완료가 아닙니다.** |
| `FAILED` | AWS가 명시적으로 거절했거나 요청이 전송되지 않음 |
| `UNKNOWN` | 읽기 timeout 등으로 AWS가 처리했는지 알 수 없음 |

**`ACCEPTED`는 "시작됨·중지됨"이 아닙니다.** AWS가 요청을 받았다는 뜻일 뿐입니다.
인스턴스는 `pending`을 거쳐 `running`이 되거나, `stopping`을 거쳐 `stopped`가 됩니다.
그래서 화면에도 "AWS 요청 접수 — 현재 상태는 새로고침으로 확인"이라고 표시합니다. **EC2 새로고침**으로 실제 상태를 확인하세요.

### `UNKNOWN`·`REQUESTED` 대응

이 두 상태의 이력이 있는 인스턴스는 새 제어가 `409 ACTION_IN_PROGRESS`로 거절됩니다. 같은 동작이 두 번 실행되는 것을 막기 위해서입니다.
자동으로 풀리지 않으므로 다음 순서로 정리합니다.

1. **같은 요청을 새 키로 다시 보내지 마세요.**
2. AWS 콘솔이나 CLI에서 실제 상태와 상태 변경 시각을 확인합니다.
3. 확인한 결과로 DB의 이력을 고칩니다.

   ```sql
   -- 실제로 시작·중지된 경우
   UPDATE action_logs SET status = 'ACCEPTED', message = '콘솔에서 수동 확인' WHERE id = '<action_id>';
   -- 실행되지 않은 경우
   UPDATE action_logs SET status = 'FAILED',   message = '콘솔에서 미실행 확인' WHERE id = '<action_id>';
   ```

화면에서 응답을 받지 못한 경우(서버 연결 끊김 등)에는 브라우저가 그 `request_id`를 메모리에 보관합니다.
**같은 요청 다시 보내기**를 누르면 같은 키로 보내므로 중복 실행되지 않습니다.

## 보안 그룹 점검

### 대상

- `pending`, `running`, `stopping`, `stopped` 상태인 EC2의 **모든 네트워크 인터페이스(ENI)에 실제로 연결된 보안 그룹만** 점검합니다.
- 어디에도 연결되지 않은 보안 그룹은 점검하지 않습니다. 보안 그룹 탭에는 이런 그룹까지 모두 나옵니다.
- 제어 허용 목록은 시작·중지 제한일 뿐, 점검 대상을 거르는 데 쓰지 않습니다.

### 판정 기준

인바운드 규칙이 다음 두 조건을 **모두** 만족하면 **"공개 관리 포트 규칙: 검토 필요"**로 표시합니다.

1. 출발지가 `0.0.0.0/0`(IPv4) 또는 `::/0`(IPv6)이다.
2. TCP 포트 범위에 22(SSH) 또는 3389(RDP)가 포함되거나, 전체 프로토콜(`-1`)을 허용한다.

- 포트는 범위로 확인합니다. 예를 들어 20~30 규칙도 22를 포함하므로 해당합니다.
- UDP와 ICMP는 판정하지 않습니다.
- 건수는 **규칙 × 공개 CIDR** 단위로 셉니다.
  - 한 규칙에 22와 3389가 모두 포함되면 1건입니다.
  - 같은 그룹을 인스턴스 여러 대가 써도 늘어나지 않습니다. 대신 표에서는 연결된 인스턴스와 ENI를 펼쳐서 보여줍니다.

### 결과 구분

| 표시 | 의미 |
|---|---|
| 공개 관리 포트 규칙: 검토 필요 N건 | 조건에 맞는 규칙이 있음 |
| 이번 점검 조건에 해당하는 규칙 없음 | 연결 그룹을 모두 점검했고 해당 규칙이 없음. **전체 환경이 안전하다는 뜻은 아님** |
| 점검 대상 EC2 연결 보안 그룹 없음 | 대상 상태의 EC2가 없어 점검할 그룹이 없음 |
| 점검 실패: … (오류 코드) | 권한 부족, 로그인 만료, 통신 실패, 조회 중 그룹 삭제 등. **결과가 없다는 뜻이며 "발견 없음"이 아님** |

일부라도 조회에 실패하면 결과 전체를 실패로 처리합니다. 오류 코드는 `AWS_ACCESS_DENIED`, `AWS_LOGIN_REQUIRED`, `AWS_CONNECTION_ERROR`, `AWS_SCAN_INCOMPLETE` 중 하나입니다.
점검을 다시 실행하면 이전 결과를 지우고 새로 조회합니다.

### 한계

- **규칙 기반 점검이며 실제 침해 여부 판정이 아닙니다.**
- 실제 접속 가능 여부는 공인 IP, 라우팅, NACL, OS 방화벽, 서비스 수신 상태에도 달려 있습니다.
- 여러 CIDR의 조합(예: `0.0.0.0/1` + `128.0.0.0/1`), prefix list, 다른 보안 그룹 참조를 통한 공개는 판정하지 않습니다.
- 점검 결과는 DB에 저장하지 않습니다.

## API

모든 `/api` 경로는 `Authorization: Bearer <앱 토큰>`이 필요합니다. 오류 본문은 항상 `{"detail": {"code": "...", "message": "..."}}` 형태입니다.

| 메서드·경로 | 설명 |
|---|---|
| `GET /api/context` | 계정 ID, 호출자 ARN, 리전 |
| `GET /api/ec2` | EC2 목록 (`controllable` 포함) |
| `GET /api/s3` | 버킷 이름 목록 |
| `GET /api/security-groups` | 리전 전체 보안 그룹 (`GroupId`, `GroupName`, `IpPermissions`) |
| `GET /api/actions` | 최근 50건 제어 이력 |
| `POST /api/ec2/{instance_id}/actions` | 본문 `{"action": "start" 또는 "stop", "request_id": "<UUID>"}`. 최초 접수는 202, 재전송은 200 |
| `GET /api/security-checks` | 보안 그룹 점검 결과 |

## 테스트

```bash
.venv/bin/python -m unittest discover -s tests -t . -v
```

AWS client와 DB 저장소를 가짜로 바꿔 실행합니다. 실제 AWS, 실습 DB, `.env`는 사용하지 않습니다.

## 확인 현황

### 직접 확인한 기능 (실제 AWS·DB·브라우저)

- `aws login --profile lab` 후 CLI 조회
- `/api/context`, `/api/ec2`, `/api/s3`, `/api/security-groups` 조회와 화면 표시
- **앱 토큰**
  - 틀린 토큰은 거절, 맞는 토큰으로 연결
  - 로그아웃하면 데이터 삭제
  - 토큰 없는 `curl` 요청은 401
- EC2 표의 **제어 허용** 열이 `.env` 허용 목록과 일치
- `init_db`로 `action_logs` 생성(InnoDB, utf8mb4, `datetime(6)`), `/api/actions` 0건 응답
- **EC2 제어**
  - 확인창을 취소하면 이력이 생기지 않음
  - 승인하면 `ACCEPTED` 기록, 새로고침하면 `pending`을 거쳐 `running`
  - 중지도 같은 흐름
  - 허용 목록 밖 인스턴스는 버튼이 꺼짐
- 보안 점검 결과가 AWS 콘솔의 인바운드 규칙과 일치
- 서버 API 결과와 AWS CLI 결과 대조
  - EC2 ID·상태, S3 버킷, 보안 그룹 수, 점검 대상 인스턴스·그룹·연결, finding 내용이 모두 일치
  - 실제 환경에서는 연결 그룹 6개에 TCP 22 / `0.0.0.0/0` 규칙 6건
- 6개 탭 구성, 요약 숫자, EC2 상태 필터, 제어 후 접수 문구
- 연결하면 토큰 입력 화면이 사라지고, 로그인이 유효하면 AWS 로그인 배너가 숨겨짐

### 미검증 기능 (단위 테스트·모의 AWS로만 확인)

실제 환경에서는 일어나지 않았거나 일부러 만들지 않은 상황입니다. 코드와 테스트로는 확인했지만, 실제 AWS나 브라우저에서 재현해 보지는 않았습니다.

- **EC2 제어 이상 상황**
  - 응답 유실 후 **같은 요청 다시 보내기**
  - 읽기 timeout으로 `UNKNOWN`이 기록되는 경우
  - AWS 접수 후 DB 갱신 실패(`DB_UPDATE_FAILED`)
  - 같은 대상에 동시 요청
- **AWS 오류 응답**
  - 실제 AWS 로그인 만료 시 `AWS_LOGIN_REQUIRED` 배너
  - 권한 부족 `AWS_ACCESS_DENIED` (관리자 권한이라 발생하지 않음)
  - 계정 불일치 `AWS_ACCOUNT_MISMATCH`
- **보안 점검**
  - 점검 중 보안 그룹 삭제 등 `AWS_SCAN_INCOMPLETE`
  - ENI·보안 그룹 정보가 누락된 응답
  - 보안 그룹 100개 초과 시 나눠 조회
  - 실제 데이터에 없는 규칙의 판정: `::/0`, 3389, 전체 프로토콜(`-1`), 20~30 같은 포트 범위
  - "이번 점검 조건에 해당하는 규칙 없음", "점검 대상 EC2 연결 보안 그룹 없음" 화면 (실제 환경에 발견이 있고 EC2가 있어서 나타나지 않음)
- **DB**
  - MariaDB 중지 시 `DB_UNAVAILABLE`

## 보안 메모

- 비밀값(`.env`, DB 암호, 앱 토큰, AWS 자격증명)은 Git, 로그, 화면, AI 대화에 넣지 않습니다.
- 서버 로그와 오류 응답에는 DB 암호, 접속 URL, 토큰을 출력하지 않습니다.
- 정적 파일은 `frontend/` 폴더만 공개합니다. 프로젝트 루트(`.env` 포함)는 웹으로 열리지 않습니다.
