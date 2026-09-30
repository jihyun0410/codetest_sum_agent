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
                                   └─HTTP──▶  /api/v1/tests/…   Agent  LLM 판단
                                             같은 프로세스 · 같은 포트
```

**CLI 는 아무것도 바뀌지 않습니다.** 명령도, `CODETEST_SERVER_URL` 이 가리키는
`…/mcp` 경로도 예전과 같습니다.

## 역할 분담

정의서: *"LLM을 사용하여 판단하는 부분은 Agent, 코드 기반으로 단순 처리 및 판단을
진행하는 부분은 MCP로 구분하여 **Fast API를 통해 송/수신**하는 방식으로 구현"*

한 프로세스가 되었어도 **그 경계와 FastAPI 송·수신은 유지합니다.** MCP 는 여전히
`agent_client` 로 Agent 의 FastAPI 를 호출하고, 주소만 자기 자신을 가리킵니다.

| | 패키지 | 하는 일 |
|---|---|---|
| **MCP** | `codetest_mcp/` | Git Diff·AST 변경 단위 식별, 프로젝트 개요 DB, 기능 중요도 판단, `@SpringBootTest` 주입, 결과 집계 |
| **Agent** | `codetest_agent/` | 변경 의도 파악, 사고의 사슬, Test Code 작성, 실행 결과 적절성 판단 |
| **결합** | `codetest_sum/` | 두 ASGI 앱을 하나로 묶어 한 포트에 올린다 |

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

합쳤다고 분리 운영을 막지는 않습니다. 예전 방식이 그대로 살아 있습니다.

```bash
uvicorn codetest_agent.main:app --host 0.0.0.0 --port 8000   # Agent 만
CODETEST_MCP_AGENT_BASE_URL=http://<agent-host>:8000 \
  python -m codetest_mcp                                     # MCP 만
```

`CODETEST_MCP_AGENT_BASE_URL` 을 지정하지 않으면 결합 서버가 자기 자신
(`http://127.0.0.1:<CODETEST_SUM_PORT>`)을 채워 넣습니다.

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
| MCP → Agent | `CODETEST_MCP_AGENT_STREAM_IDLE` (기본 180초) | Agent 가 `CODETEST_LLM_PING_SECONDS` 간격으로 `{"type":"ping"}` 을 흘린다 |

스트림은 **끝 줄 없이 끝나지 않습니다.** 어떤 실패든 `result`/`error` 한 줄로
끝맺습니다 — 잘린 스트림은 호출자에게 `RemoteProtocolError: peer closed connection
without sending complete message body` 만 남기고 원인을 하나도 알려 주지 않기 때문입니다.

## 테스트

```bash
python -m pytest -q          # 115건
```

| 경로 | 무엇을 |
|---|---|
| `tests/agent/` | Agent 계약 — LLM 을 스텁으로 두고 프롬프트에 실리는 사실과 응답 키를 검증 |
| `tests/mcp/` | MCP — 변경 단위 식별, 중요도, `@SpringBootTest` 주입, 커밋 스냅샷, Agent 스트림 |

LLM 은 스텁으로 대체하므로 API Key 없이 전부 돌아갑니다.
