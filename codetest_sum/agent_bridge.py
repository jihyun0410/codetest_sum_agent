"""MCP → Agent 를 HTTP 없이 같은 프로세스에서 잇는다.

분리 배포에서는 이 구간이 FastAPI 였다.

    MCP  ──POST /api/v1/tests/generate──▶  Agent 라우터 ──▶ testgen.generate()
         ◀──── NDJSON {"type":"result","data":…} ────────────┘

통합 배포에서는 그 사이를 그대로 걷어내고 `testgen` 을 직접 부른다.

    MCP  ──▶ AgentBridge.generate() ──▶ testgen.generate()

**주고받는 값은 같아야 한다.** 그래서 HTTP 경로가 하던 일을 여기서 그대로 한다.

  1. 요청 검증        FastAPI 라우터가 하던 "analysis 가 비었는지" 검사 (422)
  2. 응답 직렬화      pydantic 모델 → JSON 타입 dict. MCP 는 `judged.get(...)` 로
                      읽으므로 모델 객체를 그대로 주면 동작이 달라진다.
  3. 오류 변환        LLMUnavailableError → 503, LLMRefusalError → 422 를
                      `AgentError(status_code=…)` 로. HTTP 경로에서 MCP 가 받던
                      것과 같은 예외·같은 상태 코드여야 상위 처리가 같아진다.

없어지는 것은 통신뿐이다 — keep-alive ping, 스트림 절단, 연결 실패는 같은 프로세스
에서는 일어날 수 없는 실패라 다룰 것이 없다.
"""

from __future__ import annotations

from typing import Any

from codetest_agent import testgen
from codetest_agent.config import get_logger, settings
from codetest_agent.llm import LLMRefusalError, LLMUnavailableError
from codetest_mcp.agent_client import AgentError

logger = get_logger(__name__)

#: HTTP 경로에서 FastAPI 가 붙여 주던 상태 코드
_STATUS = {LLMUnavailableError: 503, LLMRefusalError: 422}


def _as_dict(payload: Any) -> dict:
    """pydantic 모델을 HTTP 응답과 같은 모양의 dict 로.

    `mode="json"` 이어야 datetime·Enum 이 HTTP 를 탄 것과 같은 값이 된다.
    """
    if hasattr(payload, "model_dump"):
        return payload.model_dump(mode="json")
    return dict(payload or {})


def _run(work, label: str) -> dict:
    """Agent 예외를 MCP 가 아는 AgentError 로 옮긴다."""
    try:
        return _as_dict(work())
    except tuple(_STATUS) as exc:
        raise AgentError(f"Agent HTTP {_STATUS[type(exc)]}: {exc}", _STATUS[type(exc)]) from None
    except Exception as exc:
        # HTTP 경로에서는 Agent 의 unhandled_exception_handler 가 500 으로 감쌌다.
        logger.exception("Agent(%s) 처리 중 예외", label)
        raise AgentError(f"Agent HTTP 500: 서버 내부 오류가 발생했습니다: {exc}", 500) from None


class AgentBridge:
    """`codetest_mcp.agent_client.LocalAgent` 구현."""

    def health(self) -> dict:
        # Agent 의 GET /api/v1/health 와 같은 본문
        return {
            "status": "ok",
            "app": settings.app_name,
            "role": "llm-based",
            "model": settings.llm_model,
        }

    def generate(
        self,
        project_id: str,
        analysis: dict,
        sources: list[dict],
        project_name: str = "",
    ) -> dict:
        # FastAPI 라우터가 하던 검증 (POST /tests/generate 의 422)
        if not analysis:
            raise AgentError(
                "Agent HTTP 422: analysis 가 비어 있습니다. "
                "MCP 의 변경 분석 결과를 함께 보내야 합니다.",
                422,
            )
        return _run(
            lambda: testgen.generate(
                analysis,
                sources=[(item["path"], item["content"]) for item in sources],
                project_name=project_name or project_id,
            ),
            "generate",
        )

    def report(
        self,
        project_id: str,
        execution: dict,
        test_code: str,
        intent: str = "",
        intent_rationale: str = "",
    ) -> dict:
        if not execution:
            raise AgentError(
                "Agent HTTP 422: execution 이 비어 있습니다. "
                "MCP 의 실행 결과를 함께 보내야 합니다.",
                422,
            )
        return _run(
            lambda: testgen.report(execution, test_code, intent, intent_rationale),
            "report",
        )
