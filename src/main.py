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

코드 기반 처리는 codetest_mcp(orchestrator 등), LLM 판단은 codetest_agent 를 같은
프로세스에서 함수로 부른다. MCP 프로토콜/fastmcp 는 쓰지 않는다.
"""

import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select

from codetest_mcp import orchestrator
from codetest_mcp.agent_client import AgentError, agent_client
from codetest_mcp.config import get_logger, settings, setup_logging, verify_api_key
from codetest_mcp.db import IngestStatus, Project, init_db, session_scope
from codetest_mcp.graph.builder import GraphBuilder
from codetest_mcp.orchestrator import FlowError, project_or_fail
from codetest_mcp.repo import RepoService, SourceFile
from codetest_mcp.schemas import ProjectRead, SourceFilePayload
from codetest_sum.agent_bridge import AgentBridge

setup_logging()
logger = get_logger(__name__)

# Agent(LLM) 는 같은 프로세스 안의 함수 호출로 부른다.
agent_client.use_local(AgentBridge())


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.ensure_directories()
    init_db()
    logger.info("%s 기동 (agent=in-process)", settings.app_name)
    yield


app = FastAPI(title="Total Test Agent", lifespan=lifespan)


def require_api_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
    """CODETEST_MCP_API_KEYS 가 비어 있으면 인증 비활성화."""
    if not verify_api_key(x_api_key):
        raise HTTPException(401, "유효하지 않은 API Key 입니다. X-API-Key 헤더를 확인하세요.")


@app.exception_handler(FlowError)
def flow_error(_, exc: FlowError):
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


# --- 백그라운드 수집 ---------------------------------------------------------
def _to_read(project: Project) -> ProjectRead:
    payload = ProjectRead.model_validate(project)
    payload.has_github_token = bool(project.github_token)
    return payload


def run_ingest(project_id: str) -> None:
    """등록 직후: AST 파싱 → Graph 적재 → 개요 DB 저장.

    CLI 가 등록 때 올린 커밋 소스 스냅샷으로 파싱하고, 없을 때만 저장소를 clone 한다.
    """
    with session_scope() as db:
        project = db.get(Project, project_id)
        if project is None:
            return

        project.ingest_status = IngestStatus.RUNNING.value
        project.ingest_error = None
        db.commit()

        stored = orchestrator.committed_sources(db, project_id)
        sources = (
            [SourceFile(path=path, content=content) for path, content in stored.items()]
            if stored
            else None
        )
        if sources is None:
            logger.info("[%s] 커밋 스냅샷이 없어 저장소를 clone 합니다 (git 필요)", project.name)

        try:
            stats = GraphBuilder(db, project).build_full(reset=True, sources=sources)
            project.ingest_status = IngestStatus.READY.value
            project.frameworks = stats.frameworks
            project.language_stats = stats.language_stats
            project.last_indexed_at = datetime.now(timezone.utc)
            db.commit()
            logger.info(
                "[%s] 개요 수집 완료 — 노드 %d, 간선 %d (%.2fs)",
                project.name, stats.node_count, stats.edge_count, stats.elapsed_seconds,
            )
        except Exception as exc:  # 어떤 실패든 상태에 남긴다
            db.rollback()
            project.ingest_status = IngestStatus.FAILED.value
            project.ingest_error = str(exc)
            db.commit()
            logger.exception("개요 수집 실패: %s", project_id)


def _ingest_async(project_id: str) -> None:
    threading.Thread(target=run_ingest, args=(project_id,), daemon=True).start()


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
    try:
        agent_client.health()
        agent = "ok"
    except AgentError as exc:
        agent = f"unreachable: {exc}"
    return {"response": f"Hello! Test Code MCP ! {name} (agent: {agent})"}


@app.post("/project/register", dependencies=[Depends(require_api_key)])
def register_project(body: RegisterBody):
    """CLI project register — 프로젝트 등록 + 개요 수집 시작.

    같은 이름·같은 git_url 로 다시 부르면 기존 프로젝트를 그대로 돌려준다
    (CLI 가 project_id 를 잃었을 때 되찾는 경로).
    """
    if not body.git_url.startswith(("http://", "https://", "git@")):
        raise HTTPException(400, "git_url 은 http(s):// 또는 git@ 형식이어야 합니다.")

    url = body.git_url.rstrip("/")
    pairs = orchestrator.as_pairs(body.sources)

    with session_scope() as db:
        existing = db.scalar(select(Project).where(Project.name == body.name))
        if existing is not None:
            if existing.git_url != url:
                raise HTTPException(
                    400,
                    f"이미 같은 이름의 프로젝트가 있고 git_url 이 다릅니다: {body.name}\n"
                    f"  등록된 주소: {existing.git_url}\n"
                    f"  요청한 주소: {url}\n"
                    "  다른 이름으로 등록하거나 기존 프로젝트를 삭제하세요.",
                )
            orchestrator.store_committed_sources(db, existing.id, pairs)  # 스냅샷 갱신
            if existing.ingest_status == IngestStatus.FAILED.value:
                _ingest_async(existing.id)  # 지난 수집 실패 → 다시 시작
            return _to_read(existing)

        project = Project(
            name=body.name,
            git_url=url,
            owner=body.owner,
            github_token=body.github_token,
            default_branch=body.default_branch,
            ingest_status=IngestStatus.PENDING.value,
        )
        db.add(project)
        db.commit()
        db.refresh(project)
        orchestrator.store_committed_sources(db, project.id, pairs)
        _ingest_async(project.id)
        return _to_read(project)


@app.delete("/project/{project_id}", dependencies=[Depends(require_api_key)])
def delete_project(project_id: str):
    """CLI project delete — 프로젝트·그래프·작업 사본 삭제."""
    with session_scope() as db:
        project = project_or_fail(db, project_id)
        repo = RepoService(project.id, project.git_url, project.github_token)
        db.delete(project)
        db.commit()
    repo.remove()
    return {"deleted": project_id}


@app.post("/generate", dependencies=[Depends(require_api_key)])
def test_generate(body: GenerateBody):
    """CLI generate — 변경 의도 파악 + 테스트 코드 생성."""
    return orchestrator.test_generate(body.project_id, body.diff, body.sources)


@app.post("/prepare", dependencies=[Depends(require_api_key)])
def prepare_test(body: PrepareBody):
    """CLI run/test 1단계 — @SpringBootTest 주입 + 저장 경로 계산."""
    return orchestrator.prepare_test(body.project_id, body.test_code, body.base_package)


@app.post("/report", dependencies=[Depends(require_api_key)])
def report_execution(body: ReportBody):
    """CLI run/test 2단계 — 로컬 실행 결과 리포트."""
    return orchestrator.report_execution(
        body.project_id, body.execution, body.test_code, body.diff,
        body.sources, body.intent, body.intent_rationale,
    )
