"""결합 서버 설정.

MCP(`codetest_mcp.config`)와 Agent(`codetest_agent.config`)의 설정은 **그대로 둔다** —
두 서비스의 동작을 바꾸지 않으려면 각자의 환경변수를 그대로 읽어야 한다. 여기서는
"둘을 한 프로세스에서 어느 포트로 띄울지" 만 정한다.

  CODETEST_SUM_HOST / CODETEST_SUM_PORT   결합 서버가 듣는 주소 (기본 0.0.0.0:80)
  CODETEST_SUM_MCP_PATH                   MCP 엔드포인트 경로 (기본 /mcp)

MCP 는 Agent 를 **프로세스 내부에서 직접** 호출한다 (`codetest_sum/agent_bridge.py`).
HTTP 를 타지 않으므로 Agent 주소 설정이 필요 없다.
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
