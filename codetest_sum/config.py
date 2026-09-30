"""결합 서버 설정.

MCP(`codetest_mcp.config`)와 Agent(`codetest_agent.config`)의 설정은 **그대로 둔다** —
두 서비스의 동작을 바꾸지 않으려면 각자의 환경변수를 그대로 읽어야 한다. 여기서는
"둘을 한 프로세스에서 어느 포트로 띄울지" 만 정한다.

  CODETEST_SUM_HOST / CODETEST_SUM_PORT   결합 서버가 듣는 주소 (기본 0.0.0.0:80)
  CODETEST_SUM_MCP_PATH                   MCP 엔드포인트 경로 (기본 /mcp)

MCP 는 Agent 를 **FastAPI 로** 호출한다 (정의서: "Fast API를 통해 송/수신"). 한
프로세스가 되었어도 그 경로는 유지하고, 주소만 자기 자신을 가리키게 한다.
"""

from __future__ import annotations

import os

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 80
DEFAULT_MCP_PATH = "/mcp"


def host() -> str:
    return os.getenv("CODETEST_SUM_HOST") or DEFAULT_HOST


def port() -> int:
    raw = os.getenv("CODETEST_SUM_PORT")
    return int(raw) if raw else DEFAULT_PORT


def mcp_path() -> str:
    path = os.getenv("CODETEST_SUM_MCP_PATH") or DEFAULT_MCP_PATH
    return "/" + path.strip("/")


def point_mcp_at_this_process() -> str:
    """MCP 가 부를 Agent 주소를 이 프로세스로 맞춘다.

    **`codetest_mcp` 를 import 하기 전에** 불러야 한다 — 설정은 import 시점에
    환경변수에서 읽히고, `agent_client` 싱글턴이 그 값을 그대로 물고 만들어진다.
    사용자가 `CODETEST_MCP_AGENT_BASE_URL` 을 직접 지정했으면 건드리지 않는다
    (Agent 를 따로 떼어 띄우는 구성을 막지 않기 위해).
    """
    existing = os.getenv("CODETEST_MCP_AGENT_BASE_URL")
    if existing:
        return existing
    url = f"http://127.0.0.1:{port()}"
    os.environ["CODETEST_MCP_AGENT_BASE_URL"] = url
    return url
