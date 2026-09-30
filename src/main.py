"""CLI command 수신 서버 — command 마다 @app.<method>() 라우트 하나.

    uvicorn src.main:app --host 0.0.0.0 --port 80

CLI command            라우트
  (연결 확인)            GET    /
  hello                  GET    /hello?name=
  project register       POST   /project/register
  project delete         DELETE /project/{project_id}
  generate               POST   /generate
  run/test 1단계         POST   /prepare
  run/test 2단계         POST   /report

처리 로직은 codetest_mcp 의 함수를 그대로 부른다 (MCP 프로토콜은 쓰지 않는다).
"""

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from fastmcp.exceptions import ToolError

from codetest_mcp import main as tools
from codetest_mcp.agent_client import agent_client
from codetest_mcp.config import settings, verify_api_key
from codetest_mcp.schemas import SourceFilePayload
from codetest_sum.agent_bridge import AgentBridge
from pydantic import BaseModel

# Agent(LLM) 는 같은 프로세스 안의 함수 호출로 부른다.
agent_client.use_local(AgentBridge())


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.ensure_directories()
    tools.init_db()
    yield


app = FastAPI(title="Total Test Agent", lifespan=lifespan)


def require_api_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
    if not verify_api_key(x_api_key):
        raise HTTPException(401, "유효하지 않은 API Key 입니다. X-API-Key 헤더를 확인하세요.")


@app.exception_handler(ToolError)
def tool_error(_, exc: ToolError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


# --- 요청 본문 ---------------------------------------------------------------
class RegisterBody(BaseModel):
    name: str
    git_url: str
    owner: str
    github_token: str | None = None
    default_branch: str = "main"
    sources: list[SourceFilePayload] = []


class GenerateBody(BaseModel):
    project_id: str
    diff: str = ""
    sources: list[SourceFilePayload] = []


class PrepareBody(BaseModel):
    project_id: str
    test_code: str
    base_package: str | None = None


class ReportBody(BaseModel):
    project_id: str
    execution: dict
    test_code: str = ""
    diff: str = ""
    sources: list[SourceFilePayload] = []
    intent: str = ""
    intent_rationale: str = ""


# --- command -----------------------------------------------------------------
@app.get("/")
def root():
    """
    This is an api that says, "Hello!"
    """
    return {"response": "Hello ! Total Test Agent !"}


@app.get("/hello", dependencies=[Depends(require_api_key)])
def hello(name: str):
    """CLI hello — 연결 확인 (Agent 상태 포함)."""
    return {"response": tools.hello(name)}


@app.post("/project/register", dependencies=[Depends(require_api_key)])
def register_project(body: RegisterBody):
    """CLI project register — 프로젝트 등록 + 개요 수집 시작."""
    return tools.register_project(**body.model_dump())


@app.delete("/project/{project_id}", dependencies=[Depends(require_api_key)])
def delete_project(project_id: str):
    """CLI project delete."""
    return tools.delete_project(project_id)


@app.post("/generate", dependencies=[Depends(require_api_key)])
def test_generate(body: GenerateBody):
    """CLI generate — 변경 의도 파악 + 테스트 코드 생성."""
    return tools.test_generate(**body.model_dump())


@app.post("/prepare", dependencies=[Depends(require_api_key)])
def prepare_test(body: PrepareBody):
    """CLI run/test 1단계 — @SpringBootTest 주입 + 저장 경로 계산."""
    return tools.prepare_test(**body.model_dump())


@app.post("/report", dependencies=[Depends(require_api_key)])
def report_execution(body: ReportBody):
    """CLI run/test 2단계 — 로컬 실행 결과 리포트."""
    return tools.report_execution(**body.model_dump())
