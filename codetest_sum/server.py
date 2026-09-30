"""MCP + Agent 를 하나의 ASGI 앱으로 묶는다.

    CLI ──HTTP/SSE──▶ /mcp        (MCP: 코드 기반 처리)
                          │
                          └─HTTP──▶ /api/v1/tests/…  (Agent: LLM 판단)
                                    같은 프로세스, 같은 포트

두 서비스의 코드는 하나도 고치지 않았다. Agent 는 여전히 FastAPI 로 받고, MCP 는
여전히 `agent_client` 로 FastAPI 를 호출한다 (정의서: "Fast API를 통해 송/수신").
달라진 것은 **어디에 배포되는가** 뿐이다 — 저장소 하나, 프로세스 하나, 포트 하나.

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

    **import 를 함수 안에서 한다.** `codetest_mcp` 는 import 시점에 설정을 읽고
    `agent_client` 싱글턴을 그 값으로 만들어 버리므로, Agent 주소를 이 프로세스로
    맞추는 일이 그보다 먼저 일어나야 한다. 모듈 최상단에 두면 그 순서가 import 문
    배치에 의존하게 되고, 정렬 도구가 한 번 섞으면 조용히 깨진다.

    바깥 앱을 Agent 로 삼는 이유는 그쪽이 이미 `/api/v1/...` 경로를 쥐고 있어서다.

    **MCP 는 `/mcp` 를 자기 내부 경로로 갖게 만들어 루트에 마운트한다.** `/mcp` 에
    마운트하고 내부 경로를 `/` 로 두면 Starlette 가 `/mcp` → `/mcp/` 로 307 을
    돌려주는데, CLI 의 httpx 는 리다이렉트를 따라가지 않아 빈 응답으로 읽힌다
    ("initialize: 서버가 빈 응답을 보냈습니다"). 분리 운영할 때의 경로와도 같아야 한다.

    루트 마운트가 Agent 를 가리지는 않는다 — Starlette 는 등록 순서로 매칭하고,
    Agent 의 라우트는 import 시점에 이미 등록돼 있어 이 마운트보다 앞에 온다.
    """
    agent_base_url = config.point_mcp_at_this_process()

    from codetest_agent.config import get_logger
    from codetest_agent.main import app
    from codetest_mcp.main import mcp

    logger = get_logger(__name__)
    path = config.mcp_path()
    mcp_app = mcp.http_app(path=path)
    agent_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(scope_app):
        # 둘 다 필요하다 — MCP 는 세션 매니저를, Agent 는 기동 로그를 여기서 처리한다
        async with mcp_app.router.lifespan_context(mcp_app), agent_lifespan(scope_app):
            logger.info(
                "결합 서버 — MCP=%s, Agent=/api/v1, Agent 호출 주소=%s",
                path, agent_base_url,
            )
            yield

    app.router.lifespan_context = lifespan
    app.mount("/", mcp_app)
    return app


def serve() -> None:
    import uvicorn

    uvicorn.run(build_app(), host=config.host(), port=config.port())
