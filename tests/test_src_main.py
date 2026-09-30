"""src.main:app 이 command 별 REST 라우트로 CLI 입력을 수신하는지 검증한다."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from codetest_mcp import db as db_module
from codetest_mcp.config import settings
from codetest_mcp.db import Base
from codetest_mcp import main as tools


@pytest.fixture
def client(monkeypatch, tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path/'t.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db_module, "SessionLocal", sessionmaker(bind=engine, expire_on_commit=False))
    monkeypatch.setattr(tools, "init_db", lambda: None)
    monkeypatch.setattr(tools, "run_ingest", lambda project_id: None)  # clone 생략
    monkeypatch.setattr(settings, "api_keys", [])
    from src.main import app

    with TestClient(app) as c:
        yield c


def test_commands_received(client):
    assert client.get("/").json() == {"response": "Hello ! Total Test Agent !"}
    assert "Hello! Test Code MCP ! CLI" in client.get("/hello", params={"name": "CLI"}).json()["response"]

    reg = client.post("/project/register", json={
        "name": "demo", "git_url": "https://github.com/a/b", "owner": "me",
        "sources": [{"path": "A.java", "content": "class A {}"}],
    })
    assert reg.status_code == 200, reg.text
    pid = reg.json()["id"]

    prep = client.post("/prepare", json={
        "project_id": pid,
        "test_code": "package com.x;\nclass ATest { }",
    })
    assert prep.status_code == 200, prep.text
    assert "@SpringBootTest" in prep.text

    # 빈 analysis/실행 결과 → 도구 오류가 400 으로 온다 (명령이 도달했다는 증거)
    assert client.post("/generate", json={"project_id": "없는id"}).status_code == 400
    assert client.post("/report", json={"project_id": "없는id", "execution": {}}).status_code == 400

    assert client.delete(f"/project/{pid}").json() == {"deleted": pid}
    assert client.post("/register-x").status_code == 404
