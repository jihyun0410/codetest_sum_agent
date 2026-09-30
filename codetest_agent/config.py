"""Agent 설정 · 로깅 · API Key 인증.

Agent 는 정의서의 "LLM을 사용하여 판단하는 부분" 만 담당한다.
진입점은 MCP 이고, 코드 기반 처리(AST/개요/중요도/실행)는 MCP 가 끝낸 뒤
LLM 이 필요한 부분만 FastAPI 로 넘겨준다. 그래서 DB·작업 디렉터리 설정이 없다.

비밀값(OPENAI_API_KEY, API Key)은 코드에 두지 않고 .env / OS 환경변수로만 주입한다.
"""

from __future__ import annotations

import hmac
import logging
import sys
from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_name: str = Field(default="Code Test AI Agent", alias="CODETEST_APP_NAME")
    host: str = Field(default="0.0.0.0", alias="CODETEST_HOST")
    port: int = Field(default=8000, alias="CODETEST_PORT")

    #: MCP 인증용 키 목록. X-API-Key 헤더와 대조한다.
    #: 비어 있으면 인증 비활성화(로컬 개발 편의).
    #: 수정: NoDecode 가 없으면 pydantic-settings 가 env 값을 JSON 으로 먼저 파싱해
    #:      아래 _split_csv 가 돌기도 전에 SettingsError 로 죽는다.
    api_keys: Annotated[list[str], NoDecode] = Field(
        default_factory=list, alias="CODETEST_API_KEYS"
    )

    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    #: LLM 접속 주소. 사내 게이트웨이·프록시·Azure 등 OpenAI 호환 엔드포인트를 쓸 때 지정한다.
    #: 비우면 SDK 기본값(https://api.openai.com/v1).
    openai_base_url: str | None = Field(default=None, alias="OPENAI_BASE_URL")
    llm_model: str = Field(default="gpt-5", alias="CODETEST_LLM_MODEL")
    #: 출력 토큰 상한. 생성 시간은 이 값에 가장 크게 좌우된다.
    llm_max_tokens: int = Field(default=32000, alias="CODETEST_LLM_MAX_TOKENS")

    #: 생성 응답을 기다리는 동안 keep-alive 를 흘려보내는 간격(초).
    #: 앞단 리버스 프록시(nginx)의 proxy_read_timeout 은 "총 소요 시간" 이 아니라
    #: **무응답 시간** 이다. 기본값 60초 동안 한 바이트도 안 오면 504 를 만든다.
    #: LLM 이 생각하는 동안 주기적으로 한 줄씩 보내면 그 타이머가 계속 초기화되어
    #: 프록시 설정을 건드리지 않고도 504 를 없앨 수 있다.
    llm_ping_seconds: float = Field(default=10.0, alias="CODETEST_LLM_PING_SECONDS")

    @field_validator("api_keys", mode="before")
    @classmethod
    def _split_csv(cls, value):
        """list 타입 환경변수는 "a,b,c" CSV 를 허용한다."""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value



@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


settings = get_settings()


# ---------------------------------------------------------------------------
def setup_logging(level: int = logging.INFO) -> None:
    """uvicorn 과 충돌하지 않도록 루트 로거에 StreamHandler 를 한 번만 붙인다."""
    root = logging.getLogger()
    if root.handlers:
        root.setLevel(level)
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
    )
    root.addHandler(handler)
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def verify_api_key(provided: str | None) -> bool:
    """settings.api_keys 가 비어 있으면 인증 비활성화. 비교는 타이밍 공격 방지."""
    allowed = settings.api_keys
    if not allowed:
        return True
    if not provided:
        return False
    return any(hmac.compare_digest(provided, key) for key in allowed)
