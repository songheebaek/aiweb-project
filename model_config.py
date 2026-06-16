"""LLM provider router.

`app.py` imports this module only. The actual provider implementation lives in:
- model_config_gemini.py
- model_config_openai.py

Select provider with LLM_PROVIDER=gemini|openai.
"""

import importlib
import os

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gemini").strip().lower()

_PROVIDER_MODULES = {
    "gemini": "model_config_gemini",
    "google": "model_config_gemini",
    "openai": "model_config_openai",
    "gpt": "model_config_openai",
}

_module_name = _PROVIDER_MODULES.get(LLM_PROVIDER)
if not _module_name:
    supported = ", ".join(sorted(_PROVIDER_MODULES))
    raise RuntimeError(
        f"지원하지 않는 LLM_PROVIDER={LLM_PROVIDER!r} 입니다. "
        f"지원값: {supported}"
    )

try:
    _provider = importlib.import_module(_module_name)
except ModuleNotFoundError as exc:
    missing = exc.name or "provider SDK"
    raise RuntimeError(
        f"{LLM_PROVIDER!r} provider를 사용하려면 누락된 패키지({missing})가 필요합니다. "
        "먼저 `pip install -r requirements.txt`를 실행하세요."
    ) from exc

# Public metadata used by the Streamlit footer/logging.
LLM_PROVIDER_NAME = getattr(_provider, "LLM_PROVIDER_NAME", LLM_PROVIDER)
LLM_MODEL = getattr(_provider, "LLM_MODEL", "unknown")

# Backward-compatible names for older UI/code paths.
GEMINI_MODEL = getattr(_provider, "GEMINI_MODEL", LLM_MODEL)


def summarize_and_highlight(transcript_text: str) -> dict:
    return _provider.summarize_and_highlight(transcript_text)


def answer_question(transcript_text: str, question: str, history: list) -> str:
    return _provider.answer_question(transcript_text, question, history)
