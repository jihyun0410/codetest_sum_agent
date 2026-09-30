"""OpenAI 호출 + 응답 섹션 파서.

- 모델 / 추론 강도 / 토큰 상한은 전부 .env 로 주입한다 (codetest_agent.config.Settings).
- 안전 거부는 message.refusal 과 finish_reason == "content_filter" 둘 다에서 확인한다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from codetest_agent.config import get_logger, settings

logger = get_logger(__name__)

#: "## IMPORTANCE" 같은 2단계 헤딩
_SECTION_HEADING = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


class LLMUnavailableError(RuntimeError):
    """API Key 미설정 등으로 호출이 불가능한 상태."""


class LLMRefusalError(RuntimeError):
    """안전 분류기가 요청을 거부한 경우."""

    def __init__(self, category: str | None, explanation: str | None) -> None:
        super().__init__(f"LLM 이 요청을 거부했습니다 (category={category}): {explanation}")
        self.category = category
        self.explanation = explanation


def _root_cause_name(exc: BaseException) -> str:
    """예외 사슬 맨 끝의 클래스 이름.

    openai SDK 는 전송 계층 예외를 APIConnectionError("Connection error.") 로
    감싸 버려서, 메시지만 봐서는 "닿지 못했다" 와 "받다가 끊겼다" 를 구분할 수
    없다. 진짜 원인은 __cause__ 사슬 끝에 있다. (httpx 를 import 하지 않고
    이름으로만 보는 이유는 이 모듈이 SDK 의 전송 구현에 묶이지 않게 하려는 것.)
    """
    cause = exc
    while cause.__cause__ is not None:
        cause = cause.__cause__
    return type(cause).__name__


@dataclass
class LLMResponse:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    stop_reason: str | None = None
    meta: dict = field(default_factory=dict)


class LLMClient:
    """프로세스 전역에서 재사용하는 OpenAI 클라이언트."""

    def __init__(self) -> None:
        self._client = None

    def _ensure_client(self):
        """지연 초기화. 키가 없으면 SDK 가 OPENAI_API_KEY 환경변수를 스스로 찾는다."""
        if self._client is not None:
            return self._client
        try:
            import openai
        except ImportError as exc:  # pragma: no cover
            raise LLMUnavailableError("openai SDK 가 설치되어 있지 않습니다.") from exc

        kwargs: dict = {}
        if settings.openai_api_key:
            kwargs["api_key"] = settings.openai_api_key
        # 사내 게이트웨이/Azure 등 OpenAI 호환 엔드포인트. 반드시 명시적으로 넘긴다 —
        # pydantic-settings 는 .env 를 os.environ 에 넣지 않으므로 SDK 가 스스로 못 읽는다.
        if settings.openai_base_url:
            kwargs["base_url"] = settings.openai_base_url
        try:
            self._client = openai.OpenAI(**kwargs)
        except Exception as exc:
            raise LLMUnavailableError(f"OpenAI 클라이언트 생성 실패: {exc}") from exc
        logger.info("LLM 엔드포인트 = %s (model=%s)", self._client.base_url, settings.llm_model)
        return self._client

    @property
    def available(self) -> bool:
        try:
            self._ensure_client()
            return True
        except LLMUnavailableError:
            return False

    def complete(self, system: str, user: str) -> LLMResponse:
        """
        생성 1건.

        :raises LLMUnavailableError: 클라이언트를 만들 수 없거나 호출이 실패했을 때
        :raises LLMRefusalError:     안전 분류기가 거부했을 때
        """
        client = self._ensure_client()

        # ponytail: 비스트리밍. max_completion_tokens 를 크게 잡으면 응답이 길어지므로
        #           타임아웃이 실제로 문제되면 그때 stream=True 로 바꾼다.
        completion = self._create(client, {
            "model": settings.llm_model,
            "max_completion_tokens": settings.llm_max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        })

        choice = completion.choices[0]
        # 본문을 읽기 전에 거부 여부를 먼저 확인한다.
        refusal = getattr(choice.message, "refusal", None)
        if refusal or choice.finish_reason == "content_filter":
            raise LLMRefusalError("content_filter", refusal or "안전 필터에 의해 차단되었습니다.")

        usage = completion.usage
        details = getattr(usage, "prompt_tokens_details", None)
        return LLMResponse(
            text=(choice.message.content or "").strip(),
            model=getattr(completion, "model", None) or settings.llm_model,
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            cache_read_tokens=getattr(details, "cached_tokens", 0) or 0,
            stop_reason=choice.finish_reason,
        )

    def _create(self, client, kwargs: dict):
        """호출 1건. 실패하면 **무엇을 고쳐야 하는지** 구분해서 알린다.

        연결 오류와 키/모델 오류는 원인도 조치도 전혀 다르다. 어떤 예외든 뭉뚱그리면
        연결이 막힌 경우에도 "OpenAI 호출에 실패했습니다: Connection error" 만 남는다.
        """
        import openai

        try:
            return client.chat.completions.create(**kwargs)

        except openai.BadRequestError as exc:
            raise LLMUnavailableError(f"OpenAI 가 요청을 거부했습니다 (400): {exc}") from None

        except openai.APIConnectionError as exc:
            # 아예 닿지 못한 경우와 응답 도중 끊긴 경우는 조치가 전혀 다르다.
            # SDK 는 둘 다 APIConnectionError("Connection error.") 로 덮어쓰므로
            # 원인 사슬을 직접 들춰 구분한다.
            if _root_cause_name(exc) == "RemoteProtocolError":
                raise LLMUnavailableError(
                    f"LLM 이 응답을 끝맺지 않고 연결을 끊었습니다 "
                    f"(peer closed connection without sending complete message body).\n"
                    f"  · 요청 주소: {client.base_url}\n"
                    f"  · 이 호출은 비스트리밍이라 생성이 끝날 때까지 한 바이트도 오지\n"
                    f"    않습니다. 그 시간이 게이트웨이/프록시의 무응답 타임아웃보다\n"
                    f"    길어지면 응답이 오기 전에 상대가 먼저 끊습니다.\n"
                    f"  · 게이트웨이의 read timeout 을 늘리거나, "
                    f"CODETEST_LLM_MAX_TOKENS({settings.llm_max_tokens}) 를 낮춰\n"
                    f"    생성 시간을 줄이세요.\n"
                    f"  ({exc.__cause__ or exc})"
                ) from None
            raise LLMUnavailableError(
                f"OpenAI 에 연결하지 못했습니다: {exc}\n"
                f"  · 요청 주소: {client.base_url}\n"
                f"  · 사내망이면 OPENAI_BASE_URL 로 게이트웨이 주소를 지정하세요.\n"
                f"  · 프록시가 필요하면 HTTPS_PROXY 를 설정하세요.\n"
                f"  · 키나 모델이 틀린 경우라면 이 오류가 아니라 401/404 가 옵니다."
            ) from None

        except openai.AuthenticationError as exc:
            raise LLMUnavailableError(
                f"인증에 실패했습니다 (401) — OPENAI_API_KEY 를 확인하세요.\n"
                f"  · 요청 주소: {client.base_url}\n"
                f"  · 사내 게이트웨이(LiteLLM 등)라면 그 게이트웨이가 발급한 키여야 합니다.\n"
                f"    OpenAI 원본 키를 넣으면 프록시가 자기 DB 에서 못 찾아 401 을 돌려줍니다.\n"
                f"  · OS 환경변수가 .env 보다 우선합니다 — 예전 키가 export 되어 있는지 확인하세요.\n"
                f"  ({exc})"
            ) from None

        except openai.NotFoundError as exc:
            raise LLMUnavailableError(
                f"모델을 찾지 못했습니다 (404) — CODETEST_LLM_MODEL={settings.llm_model} 과 "
                f"요청 주소({client.base_url})를 확인하세요: {exc}"
            ) from None

        except Exception as exc:
            raise LLMUnavailableError(f"OpenAI 호출에 실패했습니다: {exc}") from None


#: 애플리케이션 전역 싱글턴
llm_client = LLMClient()


def split_sections(markdown: str) -> dict[str, str]:
    """'## 제목' 기준으로 응답 본문을 나눈다."""
    result: dict[str, str] = {}
    matches = list(_SECTION_HEADING.finditer(markdown))
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        result[match.group(1).strip()] = markdown[start:end].strip()
    return result
