"""MCP + Agent 를 하나의 ASGI 앱으로 묶는다.

    CLI ──HTTP/SSE──▶ /mcp        (MCP: 코드 기반 처리)
                          │
                          └─함수 호출──▶ testgen.generate / report   (Agent: LLM 판단)
                                         같은 프로세스, HTTP 없음

MCP ↔ Agent 는 **HTTP 를 타지 않는다.** 같은 프로세스이므로 `agent_bridge` 가
`testgen` 을 직접 부른다. 주고받는 값은 FastAPI 를 탈 때와 같다 — 검증·직렬화·오류
변환을 브리지가 그대로 수행한다 (`codetest_sum/agent_bridge.py`).

Agent 의 `/api/v1/...` 라우트는 그대로 살려 둔다. MCP 는 쓰지 않지만, 헬스 확인과
Agent 를 따로 떼어 띄우는 구성을 위해 남겨 두는 편이 낫다.

따로 띄우고 싶으면 예전 방식이 그대로 살아 있다.

    uvicorn codetest_agent.main:app --host 0.0.0.0 --port 8000   # Agent 만
    CODETEST_MCP_AGENT_BASE_URL=http://<agent-host>:8000 \\
      python -m codetest_mcp                                     # MCP 만
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from codetest_sum import config


def build_app():
    """Agent 앱에 MCP 를 붙여 하나로 만든다.

    import 를 함수 안에서 한다 — 이 모듈을 불러오는 것만으로 두 서비스가 전부
    import 되지 않게 해서, 분리 배포에서 한쪽만 띄우는 경로를 막지 않는다.

    바깥 앱을 Agent 로 삼는 이유는 그쪽이 이미 `/api/v1/...` 경로를 쥐고 있어서다.

    **MCP 는 `/mcp` 를 자기 내부 경로로 갖게 만들어 루트에 마운트한다.** `/mcp` 에
    마운트하고 내부 경로를 `/` 로 두면 Starlette 가 `/mcp` → `/mcp/` 로 307 을
    돌려주는데, CLI 의 httpx 는 리다이렉트를 따라가지 않아 빈 응답으로 읽힌다
    ("initialize: 서버가 빈 응답을 보냈습니다"). 분리 운영할 때의 경로와도 같아야 한다.

    루트 마운트가 Agent 를 가리지는 않는다 — Starlette 는 등록 순서로 매칭하고,
    Agent 의 라우트는 import 시점에 이미 등록돼 있어 이 마운트보다 앞에 온다.
    """
    from codetest_agent.config import get_logger
    from codetest_agent.main import app
    from codetest_mcp.agent_client import agent_client
    from codetest_mcp.main import mcp
    from codetest_sum.agent_bridge import AgentBridge

    # MCP 가 Agent 를 HTTP 대신 직접 부르게 한다. 싱글턴의 동일성을 유지하므로
    # `orchestrator`·`main` 이 붙잡아 둔 참조가 그대로 이 경로를 탄다.
    agent_client.use_local(AgentBridge())

    logger = get_logger(__name__)
    path = config.mcp_path()
    mcp_app = mcp.http_app(path=path)
    agent_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(scope_app):
        # 둘 다 필요하다 — MCP 는 세션 매니저를, Agent 는 기동 로그를 여기서 처리한다
        async with mcp_app.router.lifespan_context(mcp_app), agent_lifespan(scope_app):
            logger.info(
                "결합 서버 — MCP=%s, Agent=/api/v1 (MCP 는 프로세스 내부로 호출)", path
            )
            yield

    app.router.lifespan_context = lifespan
    app.mount("/", mcp_app)
    return app


def serve() -> None:
    import uvicorn

    uvicorn.run(build_app(), host=config.host(), port=config.port())
