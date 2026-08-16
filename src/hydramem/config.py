"""Environment-driven configuration for the hydramem services."""

import os


BOLT_URI = os.environ.get("HYDRA_BOLT_URI", "bolt://127.0.0.1:7687")
BOLT_AUTH_TOKEN = os.environ.get("HYDRA_AUTH_TOKEN", "local-development-token-32-bytes")
DATABASE = os.environ.get("HYDRA_DATABASE", "default")

LLM_BASE_URL = os.environ.get(
    "HYDRA_MEM_LLM_BASE_URL", "https://openrouter.ai/api/v1"
).rstrip("/")
LLM_API_KEY = os.environ.get(
    "HYDRA_MEM_LLM_API_KEY", os.environ.get("OPENROUTER_API_KEY", "")
)
LLM_MODEL = os.environ.get("HYDRA_MEM_LLM_MODEL", "openai/gpt-4o-mini")
LLM_MAX_TOKENS = int(os.environ["HYDRA_MEM_LLM_MAX_TOKENS"]) if os.environ.get("HYDRA_MEM_LLM_MAX_TOKENS") else None
LLM_MODE = os.environ.get("HYDRA_MEM_LLM_MODE", "llm" if LLM_API_KEY else "mock")

MAX_PATH_LEN = int(os.environ.get("HYDRA_MEM_MAX_PATH_LEN", "3"))
PATH_COUNT = int(os.environ.get("HYDRA_MEM_PATH_COUNT", "200"))
RESULT_LIMIT = int(os.environ.get("HYDRA_MEM_RESULT_LIMIT", "2000"))

SIMILARITY_THRESHOLD = float(os.environ.get("HYDRA_MEM_SIM_THRESHOLD", "0.8"))

SAMPLE_DATA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data",
    "sample_sessions.json",
)
