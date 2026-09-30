# Code Test AI Agent — 통합 서버 (MCP + Agent)

두 저장소로 나뉘어 있던 **MCP(코드 기반 처리)** 와 **Agent(LLM 판단)** 를 한 저장소에
합쳤습니다. 코드는 그대로이고, 달라진 것은 **배포 단위** 입니다 — 저장소 하나,
프로세스 하나, 포트 하나.

```
IntelliJ Terminal
   │
   ▼
codetest CLI  ──HTTP/SSE──▶  /mcp          MCP   코드 기반 처리
(codereview_gitver)                │
                                   └─함수 호출──▶ testgen.generate / report
                                                  Agent  LLM 판단
                                                  같은 프로세스 · HTTP 없음
```

**CLI 는 아무것도 바뀌지 않습니다.** 명령도, `CODETEST_SERVER_URL` 이 가리키는
`…/mcp` 경로도 예전과 같습니다.

## 역할 분담

정의서: *"LLM을 사용하여 판단하는 부분은 Agent, 코드 기반으로 단순 처리 및 판단을
진행하는 부분은 MCP로 구분하여 Fast API를 통해 송/수신하는 방식으로 구현"*

**역할 경계는 그대로 두고, 그 사이의 통신만 걷어냈습니다.** 한 프로세스이므로
`agent_bridge` 가 `testgen` 을 직접 부릅니다 — HTTP 를 타지 않습니다. 분리 배포에서는
예전처럼 FastAPI 로 오갑니다 (`agent_client.use_local(None)` 이 기본).

| | 패키지 | 하는 일 |
|---|---|---|
| **MCP** | `codetest_mcp/` | Git Diff·AST 변경 단위 식별, 프로젝트 개요 DB, 기능 중요도 판단, `@SpringBootTest` 주입, 결과 집계 |
| **Agent** | `codetest_agent/` | 변경 의도 파악, 사고의 사슬, Test Code 작성, 실행 결과 적절성 판단 |
| **결합** | `codetest_sum/` | 두 앱을 한 포트에 올리고, MCP → Agent 를 내부 호출로 잇는다 |

기능 중요도(High/Mid/Low)는 Agent 에 묻지 않습니다. 코드 그래프로 확정하는 값이라
MCP 의 몫입니다 (`codetest_mcp/importance.py`).

**테스트 실행은 이 서버가 하지 않습니다.** CLI 가 개발자 PC 의 프로젝트에서 Gradle·
Maven 으로 돌리고 그 결과만 보내옵니다 — 그래서 이 서버에 JDK·Gradle 이 필요 없습니다.

## 실행

```bash
pip install -e .
cp .env.example .env        # 값 채우기 (OPENAI_API_KEY 등)
python -m codetest_sum      # MCP + Agent 한 프로세스, 기본 0.0.0.0:80
```

CLI 쪽 설정:

```bash
export CODETEST_SERVER_URL="http://<host>:80/mcp"
export CODETEST_API_KEY="…"        # CODETEST_MCP_API_KEYS 중 하나
```

### 따로 띄우기

합쳤다고 분리 운영을 막지는 않습니다. 그때는 예전처럼 FastAPI 로 오갑니다.

```bash
uvicorn codetest_agent.main:app --host 0.0.0.0 --port 8000   # Agent 만
CODETEST_MCP_AGENT_BASE_URL=http://<agent-host>:8000 \
  python -m codetest_mcp                                     # MCP 만
```

## MCP → Agent 를 내부 호출로 잇는 방법

`codetest_mcp/agent_client.py` 의 싱글턴에 `use_local(backend)` 로 구현을 끼웁니다.
**객체를 갈아 끼우지 않고 안을 바꿉니다** — `orchestrator`·`main` 이 import 시점에
이 객체를 붙잡아 두므로 그래야 그쪽 코드를 건드리지 않습니다.

`agent_bridge` 는 HTTP 경로가 하던 일을 그대로 합니다. 이게 전부 같아야 "통신 방식만
다르다" 가 성립합니다.

| HTTP 경로에서 누가 하던 일 | 내부 호출에서 |
|---|---|
| FastAPI 라우터의 `analysis`/`execution` 빈 값 검사 (422) | 브리지가 같은 메시지로 검사 |
| pydantic 모델 → JSON 응답 | `model_dump(mode="json")` — MCP 는 `judged.get(...)` 로 읽는다 |
| `LLMUnavailableError` → 503, `LLMRefusalError` → 422 | 같은 코드를 `AgentError.status_code` 에 실어 준다 |
| unhandled 예외 → 500 | 같은 문구로 `AgentError(…, 500)` |

없어지는 것은 통신뿐입니다 — keep-alive ping, 스트림 절단, 연결 실패는 같은
프로세스에서 일어날 수 없는 실패라 다룰 것이 없습니다.

`tests/test_bridge_equivalence.py` 가 같은 입력을 두 경로에 넣어 **응답이 바이트로
같은지** 그리고 오류 상태 코드가 같은지 검사합니다.

## 환경변수

예전 이름을 그대로 씁니다 — 설정을 옮겨 적을 필요가 없습니다. 전체 목록과 설명은
`.env.example` 에 있습니다.

| 접두사 | 대상 |
|---|---|
| `CODETEST_SUM_*` | 결합 서버 (듣는 주소, MCP 경로) |
| `CODETEST_MCP_*` | MCP (DB, 작업 디렉터리, Agent 주소·타임아웃, API Key) |
| `CODETEST_*` / `OPENAI_*` | Agent (모델, 토큰 상한, keep-alive 간격, API Key) |

## 왜 `/mcp` 를 루트에 마운트하나

`app.mount("/mcp", mcp.http_app(path="/"))` 로 붙이면 Starlette 가 `/mcp` →
`/mcp/` 로 307 을 돌려줍니다. CLI 의 httpx 는 리다이렉트를 따라가지 않아 그 307 을
빈 응답으로 읽고 `initialize: 서버가 빈 응답을 보냈습니다` 로 끊깁니다.

그래서 MCP 앱의 **내부 경로를 `/mcp` 로 만들어 루트에 마운트**합니다. 분리 운영할
때의 경로와도 같아집니다. Agent 라우트는 import 시점에 이미 등록돼 있어 이 루트
마운트보다 앞서 매칭되므로 가려지지 않습니다 (`codetest_sum/server.py`).

## 생성 대기 — 504 와 RemoteProtocolError

두 구간 모두 **무응답 시간** 으로 끊습니다. 총 소요 시간이 아닙니다.

| 구간 | 무응답 상한 | 어떻게 |
|---|---|---|
| CLI → MCP | sse-starlette 가 SSE 에 15초마다 ping | MCP 응답을 받으면 CLI 가 스트림 닫힘을 기다리지 않고 끝낸다 |
| MCP → Agent (**분리 배포만**) | `CODETEST_MCP_AGENT_STREAM_IDLE` (기본 180초) | Agent 가 `CODETEST_LLM_PING_SECONDS` 간격으로 `{"type":"ping"}` 을 흘린다 |

통합 배포에서는 MCP → Agent 구간에 네트워크가 없으므로 이 장치가 쓰이지 않습니다.

스트림은 **끝 줄 없이 끝나지 않습니다.** 어떤 실패든 `result`/`error` 한 줄로
끝맺습니다 — 잘린 스트림은 호출자에게 `RemoteProtocolError: peer closed connection
without sending complete message body` 만 남기고 원인을 하나도 알려 주지 않기 때문입니다.

## 테스트

```bash
python -m pytest -q          # 123건
```

| 경로 | 무엇을 |
|---|---|
| `tests/agent/` | Agent 계약 — LLM 을 스텁으로 두고 프롬프트에 실리는 사실과 응답 키를 검증 |
| `tests/mcp/` | MCP — 변경 단위 식별, 중요도, `@SpringBootTest` 주입, 커밋 스냅샷, Agent 스트림 |
| `tests/test_bridge_equivalence.py` | 내부 호출과 FastAPI 경로가 같은 값·같은 상태 코드를 주는지 |

LLM 은 스텁으로 대체하므로 API Key 없이 전부 돌아갑니다.
