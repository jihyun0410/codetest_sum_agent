"""Agent Server 진입점 + REST API.

정의서:
  "**LLM을 사용하여 판단하는 부분은 Agent**, 코드 기반으로 단순 처리 및 판단을
   진행하는 부분은 MCP로 구분하여 Fast API를 통해 송/수신하는 방식으로 구현"

이 서버는 **LLM 판단만** 수행한다. 진입점은 MCP 다 — CLI 명령을 받은 MCP 가
Git clone·AST·개요 저장·기능 중요도 판단·@SpringBootTest 주입·JaCoCo 실행을
코드 기반으로 끝낸 뒤, LLM 이 필요한 부분만 이 서버에 FastAPI 로 넘긴다.

    IntelliJ Terminal → CLI → MCP → Agent(이 서버) → MCP → CLI

    python -m codetest_sum                                    # MCP 와 함께 (권장)
    uvicorn codetest_agent.main:app --host 0.0.0.0 --port 8000  # Agent 만 따로

MCP 가 호출하는 엔드포인트:

  GET  /api/v1/health            연결 확인 (인증 불필요)
  POST /api/v1/tests/generate    변경 의도 파악 + 사고의 사슬 + Test Code 생성
  POST /api/v1/tests/execute     실행 결과 적절성 판단

**기능 중요도는 여기서 판단하지 않는다.** 코드 그래프로 확정하는 값이라 MCP 의 몫이다
(codetest-MCP `codetest_mcp/importance.py`). 중요도와 그 판단 근거는 MCP 가 결과에
실어 CLI 화면까지 그대로 전달한다.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, contextmanager

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from codetest_agent import testgen
from codetest_agent.config import get_logger, settings, setup_logging, verify_api_key
from codetest_agent.llm import LLMRefusalError, LLMUnavailableError
from codetest_agent.schemas import (
    ExecuteRequest,
    GenerateRequest,
    GenerateResponse,
    ReportResponse,
)

setup_logging()
logger = get_logger(__name__)


# --- 의존성 / 헬퍼 -----------------------------------------------------------
def require_api_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
    """CODETEST_API_KEYS 가 비어 있으면 인증 비활성화(로컬 개발용)."""
    if not verify_api_key(x_api_key):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "유효하지 않은 API Key 입니다. X-API-Key 헤더를 확인하세요.",
        )


@contextmanager
def _llm_errors():
    """LLM 예외를 클라이언트가 이해할 HTTP 상태로 옮긴다."""
    try:
        yield
    except LLMUnavailableError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from None
    except LLMRefusalError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None


# --- 앞단 프록시 대비 keep-alive 스트리밍 ------------------------------------
#: 한 줄에 JSON 하나. ping 줄은 버리고 마지막 result/error 줄만 쓰면 된다.
NDJSON_MEDIA_TYPE = "application/x-ndjson"


def _wants_ndjson(request: Request) -> bool:
    """호출자가 keep-alive 스트림을 받겠다고 했는가.

    Accept 로 고르게 해 두면 예전 MCP(일반 JSON 만 아는 쪽)는 그대로 동작한다.
    """
    return NDJSON_MEDIA_TYPE in (request.headers.get("accept") or "")


def _error_line(exc: Exception) -> dict:
    """스트림이 시작된 뒤에는 상태 코드를 못 바꾸므로 본문에 실어 보낸다."""
    if isinstance(exc, LLMUnavailableError):
        return {"type": "error", "status": 503, "detail": str(exc)}
    if isinstance(exc, LLMRefusalError):
        return {"type": "error", "status": 422, "detail": str(exc)}
    if isinstance(exc, HTTPException):
        return {"type": "error", "status": exc.status_code, "detail": str(exc.detail)}
    logger.exception("LLM 처리 중 예외")
    return {"type": "error", "status": 500, "detail": f"서버 내부 오류가 발생했습니다: {exc}"}


def _line(payload: dict) -> bytes:
    return (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")


async def _ndjson(work: Callable[[], object]) -> AsyncIterator[bytes]:
    """LLM 이 답할 때까지 ping 을 흘리고, 끝나면 결과 한 줄을 보낸다.

    nginx 의 proxy_read_timeout 은 총 소요 시간이 아니라 **무응답 시간**이다.
    한 줄이라도 도착하면 타이머가 처음부터 다시 시작하므로, 생성이 몇 분 걸려도
    프록시 설정을 건드리지 않고 504 를 피할 수 있다.

    **끝 줄 없이 끝나면 안 된다.** 스트림이 시작된 뒤 본문이 잘리면 호출자는
    `RemoteProtocolError: peer closed connection without sending complete
    message body (incomplete chunked read)` 를 보게 되고, 거기엔 무엇이 잘못
    됐는지가 하나도 담기지 않는다. 어떤 실패든 result/error 한 줄로 끝맺는다.
    """
    task = asyncio.create_task(run_in_threadpool(work))
    try:
        # 첫 줄을 바로 보낸다 — uvicorn 은 본문 첫 조각이 나와야 헤더를 내보내므로,
        # 이게 없으면 첫 ping 까지 앞단에 아무것도 도착하지 않는다.
        yield b'{"type":"ping"}\n'

        while True:
            done, _ = await asyncio.wait({task}, timeout=settings.llm_ping_seconds)
            if done:
                break
            yield b'{"type":"ping"}\n'

        try:
            line = _line({"type": "result", "data": jsonable_encoder(task.result())})
        except Exception as exc:      # 어떤 실패든 스트림 안에서 알려야 한다
            line = _line(_error_line(exc))
        yield line
    finally:
        # 끝 줄까지 못 가고 닫히는 경우(호출자가 끊음·서버 종료)에도 기다리던
        # 작업을 놓아 준다. 이미 끝난 task 면 아무 일도 일어나지 않는다.
        task.cancel()


def _streaming(work: Callable[[], object]) -> StreamingResponse:
    return StreamingResponse(
        _ndjson(work),
        media_type=NDJSON_MEDIA_TYPE,
        headers={
            # nginx 가 이 응답만 버퍼링하지 않게 한다 — ping 이 즉시 통과해야 한다
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
        },
    )


# --- 라우터 ------------------------------------------------------------------
router = APIRouter(prefix="/api/v1")


@router.get("/health", tags=["health"], summary="헬스체크")
def health() -> dict:
    """연결 확인용 (인증 불필요). MCP 의 `hello` 도구가 이 값을 함께 알린다."""
    return {
        "status": "ok",
        "app": settings.app_name,
        "role": "llm-based",
        "model": settings.llm_model,
    }


tests = APIRouter(
    prefix="/tests", tags=["tests"], dependencies=[Depends(require_api_key)]
)


@tests.post("/generate", response_model=GenerateResponse,
            summary="Test Code 생성 (CLI: codetest generate / run)")
async def generate_tests(payload: GenerateRequest, request: Request):
    """
    MCP 가 확정한 변경 단위·영향도를 근거로 의도를 파악하고 @SpringBootTest 를 만든다.

    코드 기반 작업은 하지 않는다 — 분석은 이미 끝난 상태로 본문에 실려 온다.

    `Accept: application/x-ndjson` 이면 생성이 끝날 때까지 ping 을 흘려보내는
    스트림으로 답한다 (앞단 프록시의 무응답 타임아웃 회피). 아니면 예전처럼
    JSON 한 덩어리로 답한다.
    """
    if not payload.analysis:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "analysis 가 비어 있습니다. MCP 의 변경 분석 결과를 함께 보내야 합니다.",
        )

    def work() -> GenerateResponse:
        return testgen.generate(
            payload.analysis,
            sources=[(item.path, item.content) for item in payload.sources],
            project_name=payload.project_name or payload.project_id,
        )

    if _wants_ndjson(request):
        return _streaming(work)
    with _llm_errors():
        return await run_in_threadpool(work)


@tests.post("/execute", response_model=ReportResponse,
            summary="실행 결과 적절성 판단 (CLI: codetest test / run)")
async def execute_tests(payload: ExecuteRequest, request: Request):
    """MCP 가 돌린 @SpringBootTest 결과를 보고 적절성을 판단한다.

    generate 와 같은 규칙으로 `Accept: application/x-ndjson` 스트림을 지원한다.
    """
    if not payload.execution:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "execution 이 비어 있습니다. MCP 의 실행 결과를 함께 보내야 합니다.",
        )

    def work() -> ReportResponse:
        return testgen.report(
            payload.execution,
            payload.test_code,
            payload.intent,
            payload.intent_rationale,
        )

    if _wants_ndjson(request):
        return _streaming(work)
    with _llm_errors():
        return await run_in_threadpool(work)


router.include_router(tests)


# --- 앱 ----------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("%s 기동 (model=%s)", settings.app_name, settings.llm_model)
    yield
    logger.info("%s 종료", settings.app_name)


app = FastAPI(
    title=settings.app_name,
    description=(
        "LLM 판단 전담 Agent. 변경 의도 파악·사고의 사슬·@SpringBootTest 생성·"
        "결과 적절성 판단을 담당한다. 코드 기반 처리와 기능 중요도 판단은 MCP 의 몫이다."
    ),
    version="0.1.0",
    lifespan=lifespan,
)
app.include_router(router)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """처리되지 않은 예외를 500 JSON 으로 정규화. 스택트레이스는 서버 로그에만 남긴다."""
    logger.exception("처리되지 않은 예외: %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "서버 내부 오류가 발생했습니다.", "error": str(exc)},
    )


@app.get("/", include_in_schema=False)
def root() -> dict:
    return {"app": settings.app_name, "docs": "/docs", "api": "/api/v1"}
