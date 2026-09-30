"""Agent REST 요청/응답 스키마.

MCP(codetest-MCP)의 `agent_client.py` 가 보내는 본문과 1:1 로 대응한다.
흐름은 CLI → MCP → Agent 다 — 이 서버를 부르는 쪽은 언제나 MCP 다.

Agent 는 **LLM 판단만** 한다. 변경 단위·영향도·기능 중요도·실행 결과는 MCP 가
코드로 확정해 본문에 실어 보내므로 여기서 다시 계산하지 않는다.

  POST /api/v1/tests/generate   MCP 가 준 분석 사실 → 의도·사고의 사슬·Test Code
  POST /api/v1/tests/execute    MCP 가 준 실행 결과 → 적절성 판단
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SourceFilePayload(BaseModel):
    path: str
    content: str


# ---------------------------------------------------------------------------
#  Test Code 생성 — 정의서 (2) 의도 파악 + (3) 생성 + [상세] 2·3 CoT
# ---------------------------------------------------------------------------
class GenerateRequest(BaseModel):
    """MCP 가 `POST /api/v1/tests/generate` 로 보내는 본문."""

    project_id: str
    #: 프롬프트에 넣을 프로젝트 이름
    project_name: str = ""
    #: MCP 가 Git Diff + AST 로 확정한 변경 분석 전문 (MCP 내부 단계 `_analyze`)
    #: (키: changed_units / impacted_units / risk / risk_reasons / frameworks /
    #:  base_package / changed_ranges / graph_ready / warnings / diff)
    analysis: dict = Field(default_factory=dict)
    #: 변경 파일 본문 (테스트 대상 코드)
    sources: list[SourceFilePayload] = Field(default_factory=list)


class GenerateResponse(BaseModel):
    """LLM 판단만 담는다. 기능 중요도는 MCP 가 코드 그래프로 확정하므로 여기 없다."""

    #: [상세 2] 사고의 사슬 — 생각하는 과정 (Test Code 작성 근거의 일부)
    thinking: str = ""
    #: (2) 파악한 변경 의도 — 기능 추가 / 조건 변경 / 성능 개선 …
    intent: str = ""
    intent_rationale: str = ""
    #: (3) 정상 케이스 / 실패 케이스 판단 결과
    test_cases: str = ""
    #: @SpringBootTest 테스트 코드
    test_code: str = ""
    rationale: str = ""
    #: 테스트 대상 코드 (CLI "Test Code 보기")
    target_code: str = ""
    #: MCP 가 준 기준 패키지를 그대로 되돌려 준다 (MCP 가 실행에 쓴다)
    base_package: str | None = None


# ---------------------------------------------------------------------------
#  적절성 판단 — 정의서 [UI] 3 + (2) 의도를 결과값에 포함
# ---------------------------------------------------------------------------
class ExecuteRequest(BaseModel):
    """MCP 가 `POST /api/v1/tests/execute` 로 보내는 본문.

    MCP 가 이미 @SpringBootTest 를 주입해 Gradle 을 돌렸다.
    Agent 는 그 결과가 적절한지만 판단한다.
    """

    project_id: str
    #: MCP `execute_tests` 실행 결과 전문
    #: (키: exit_code / passed / failed / skipped / total / failures / coverage /
    #:  jacoco_enabled / springboot_applied / applied / test_file_path / output)
    execution: dict = Field(default_factory=dict)
    #: 실행한 Test Code
    test_code: str = ""
    #: MCP 가 앞서 받아 둔 변경 의도 (결과값에 함께 표시하기 위함)
    intent: str = ""
    intent_rationale: str = ""


class ReportResponse(BaseModel):
    """LLM 판단만 담는다. 실행 집계·커버리지는 MCP 가 이미 갖고 있다."""

    #: [UI 3] 결과가 적절한지 (적절 / 부적절)
    verdict: str = ""
    verdict_rationale: str = ""
    details: str = ""

    #: (2) "파악한 의도와 근거를 <Test Result 보기>의 결과값에 넣는다"
    intent: str = ""
    intent_rationale: str = ""
