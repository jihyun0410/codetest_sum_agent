"""내부 호출 경로가 FastAPI 경로와 **같은 값**을 주는지 검증한다.

병합 배포는 MCP ↔ Agent 사이의 HTTP 를 걷어내고 `agent_bridge` 가 `testgen` 을
직접 부른다. 통신 방식만 달라야 하고 주고받는 값은 같아야 하므로, 같은 입력을
두 경로에 넣어 응답을 그대로 비교한다.

  FastAPI 경로 : TestClient → 라우터 → testgen  → JSON 응답
  내부 호출    : AgentBridge          → testgen  → dict

두 결과가 다르면 MCP 가 읽는 키·값이 달라진다는 뜻이다 (`orchestrator` 는
`judged.get(...)` 로 읽는다).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from codetest_agent.llm import LLMRefusalError, LLMResponse, LLMUnavailableError, llm_client
from codetest_agent.main import app
from codetest_mcp.agent_client import AgentError
from codetest_sum.agent_bridge import AgentBridge

GENERATE_OUTPUT = """\
## THINKING
- quantity > 10 분기가 추가되었다

## INTENT
조건 변경

## INTENT_RATIONALE
- 분기 추가

## TEST_CASES
- [정상] 11개면 할인

## TEST_CODE
```java
package com.example.demo;

class OrderServiceTest { }
```

## TEST_RATIONALE
- 경계값을 덮기 위해
"""

REPORT_OUTPUT = """\
## VERDICT
적절

## VERDICT_RATIONALE
- 분기를 모두 통과

## DETAILS
- 2 passed
"""

ANALYSIS = {
    "base_package": "com.example.demo",
    "risk": "LOW",
    "risk_score": 5,
    "changed_units": [],
    "impacted_units": [],
    "affected_files": ["src/main/java/com/example/demo/service/OrderService.java"],
    "diff": "@@ -5,6 +5,9 @@\n+        if (quantity > 10) {",
}
SOURCES = [{"path": "src/main/java/com/example/demo/service/OrderService.java",
            "content": "class OrderService { }"}]
EXECUTION = {"exit_code": 0, "passed": 2, "failed": 0, "skipped": 0, "total": 2,
             "springboot_applied": True, "output": "BUILD SUCCESSFUL"}


@pytest.fixture
def client():
    return TestClient(app)


def _stub(monkeypatch, text: str) -> None:
    monkeypatch.setattr(llm_client, "complete", lambda system, user: LLMResponse(text=text, model="stub"))


# --- 정상 응답 -----------------------------------------------------------------
def test_generate_returns_the_same_payload_both_ways(client, monkeypatch):
    _stub(monkeypatch, GENERATE_OUTPUT)

    over_http = client.post(
        "/api/v1/tests/generate",
        json={"project_id": "p1", "project_name": "demo", "analysis": ANALYSIS, "sources": SOURCES},
    )
    in_process = AgentBridge().generate("p1", ANALYSIS, SOURCES, "demo")

    assert over_http.status_code == 200
    assert in_process == over_http.json()


def test_report_returns_the_same_payload_both_ways(client, monkeypatch):
    _stub(monkeypatch, REPORT_OUTPUT)

    over_http = client.post(
        "/api/v1/tests/execute",
        json={"project_id": "p1", "execution": EXECUTION, "test_code": "class T {}",
              "intent": "조건 변경", "intent_rationale": "분기 추가"},
    )
    in_process = AgentBridge().report("p1", EXECUTION, "class T {}", "조건 변경", "분기 추가")

    assert over_http.status_code == 200
    assert in_process == over_http.json()


def test_health_matches_the_http_endpoint(client):
    assert AgentBridge().health() == client.get("/api/v1/health").json()


# --- 오류도 같은 상태 코드로 와야 한다 -------------------------------------------
#
# MCP 는 AgentError.status_code 로 갈래를 나눈다. HTTP 경로에서는 FastAPI 가
# 붙여 준 코드였으므로, 내부 호출에서도 같은 코드가 실려야 상위 처리가 같아진다.
def test_empty_analysis_is_422_both_ways(client):
    over_http = client.post(
        "/api/v1/tests/generate",
        json={"project_id": "p1", "analysis": {}, "sources": []},
    )
    assert over_http.status_code == 422

    with pytest.raises(AgentError) as exc:
        AgentBridge().generate("p1", {}, [])
    assert exc.value.status_code == 422


def test_empty_execution_is_422_both_ways(client):
    over_http = client.post(
        "/api/v1/tests/execute",
        json={"project_id": "p1", "execution": {}, "test_code": ""},
    )
    assert over_http.status_code == 422

    with pytest.raises(AgentError) as exc:
        AgentBridge().report("p1", {}, "")
    assert exc.value.status_code == 422


def test_llm_unavailable_is_503_both_ways(client, monkeypatch):
    def _boom(system, user):
        raise LLMUnavailableError("키가 없습니다")

    monkeypatch.setattr(llm_client, "complete", _boom)

    over_http = client.post(
        "/api/v1/tests/generate",
        json={"project_id": "p1", "analysis": ANALYSIS, "sources": SOURCES},
    )
    assert over_http.status_code == 503

    with pytest.raises(AgentError) as exc:
        AgentBridge().generate("p1", ANALYSIS, SOURCES)
    assert exc.value.status_code == 503
    assert "키가 없습니다" in str(exc.value)


def test_llm_refusal_is_422_both_ways(client, monkeypatch):
    def _boom(system, user):
        raise LLMRefusalError("content_filter", "차단됨")

    monkeypatch.setattr(llm_client, "complete", _boom)

    over_http = client.post(
        "/api/v1/tests/generate",
        json={"project_id": "p1", "analysis": ANALYSIS, "sources": SOURCES},
    )
    assert over_http.status_code == 422

    with pytest.raises(AgentError) as exc:
        AgentBridge().generate("p1", ANALYSIS, SOURCES)
    assert exc.value.status_code == 422


# --- MCP 가 실제로 내부 경로를 타는가 --------------------------------------------
def test_build_app_switches_the_mcp_to_the_in_process_path():
    from codetest_mcp.agent_client import agent_client
    from codetest_sum.server import build_app

    was_local = agent_client.is_local
    try:
        build_app()
        assert agent_client.is_local, "결합 서버는 MCP 를 내부 호출로 바꿔야 한다"
    finally:
        agent_client.use_local(None if not was_local else agent_client._local)
