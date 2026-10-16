"""실행 중인 서버의 조회 API를 앱 토큰과 함께 호출한다. 토큰은 출력하지 않는다.

사용: python check_api.py /api/context
      python check_api.py /api/ec2
"""

import json
import os
import sys
import urllib.error
import urllib.request

from dotenv import load_dotenv

from backend.config import ENV_FILE

BASE_URL = "http://127.0.0.1:8001"


def main() -> int:
    if len(sys.argv) != 2 or not sys.argv[1].startswith("/api/"):
        print("사용법: python check_api.py /api/<경로>   예) /api/context", file=sys.stderr)
        return 2
    path = sys.argv[1]

    load_dotenv(ENV_FILE, override=False)
    token = (os.getenv("ADMIN_API_TOKEN") or "").strip()
    if not token:
        print("ADMIN_API_TOKEN이 설정되지 않았습니다.", file=sys.stderr)
        return 1

    # 조회(GET)만 한다. 시작·중지 같은 변경 요청은 웹 화면에서 한다.
    request = urllib.request.Request(
        BASE_URL + path, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            status, body = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        status, body = exc.code, exc.read()
    except urllib.error.URLError as exc:
        print(f"서버에 연결하지 못했습니다: {exc.reason} (uvicorn이 {BASE_URL}에서 실행 중인지 확인)", file=sys.stderr)
        return 1

    print(f"HTTP {status} GET {path}")
    try:
        print(json.dumps(json.loads(body), ensure_ascii=False, indent=2))
    except ValueError:
        print(body.decode("utf-8", errors="replace"))
    return 0 if 200 <= status < 300 else 1


if __name__ == "__main__":
    sys.exit(main())
