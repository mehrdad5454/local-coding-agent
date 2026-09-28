"""
LLM Provider Layer — Phase 7 preparation only.

Goal:
    Make it possible to talk to different LLM providers in the future
    WITHOUT changing today's behaviour in any way.

Providers described here:
    - llama_cpp_local   : the CURRENT provider (llama-server.exe on
                          http://127.0.0.1:8080/completion). It is the
                          default and is described exactly as it is used
                          today — same endpoint, same request body.
    - openrouter        : OpenAI-compatible cloud gateway (chat API).
    - agentrouter       : OpenAI-compatible cloud gateway (chat API).
    - openai_compatible : generic placeholder for any other
                          OpenAI-compatible endpoint (self-hosted or cloud).

Phase 7 rules honoured by this module:
    - Nothing here is wired into the runtime path. Importing this module
      performs NO network call and mutates NO global state.
    - The current model is NOT removed: llama_cpp_local is the default spec
      and its endpoint equals agent.MODEL_URL today.
    - Prompts are NOT changed and NOT moved: each agent keeps its own
      prompts. Only the raw transport wrapper (system/user framing) is
      reproduced here so a future phase can reuse it byte-for-byte.
    - No new dependency: standard library only.
    - agent.call_model() is left untouched in Phase 7.

How a future phase activates a provider (not done in Phase 7):
    1. spec = get_provider("<id>")            # e.g. "openrouter"
    2. url = resolve_endpoint(spec)           # + env override support
    3. headers = build_headers(spec)          # adds the API key if needed
    4. body = build_payload(spec, system_prompt, user_prompt)
    5. POST with urllib, then read the answer
       - LLAMA_COMPLETION  -> result["content"]
       - OPENAI_CHAT       -> result["choices"][0]["message"]["content"]

Third-party endpoint defaults are convenience values copied from public
provider documentation. They MUST be verified before they are ever
activated, and they can always be overridden with the provider's
`base_url_env` environment variable.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional


class LLMStyle(str, Enum):
    """
    Wire format of a provider.

    LLAMA_COMPLETION:
        llama.cpp style `/completion` endpoint: a single raw prompt string.
        This is the only style used today.

    OPENAI_CHAT:
        OpenAI-compatible `/chat/completions` endpoint: a `messages` array.
        Declared for future phases; not used anywhere in Phase 7.
    """

    LLAMA_COMPLETION = "llama_completion"
    OPENAI_CHAT = "openai_chat"


# ---------------------------------------------------------------------------
# Transport constants.
#
# These intentionally MIRROR the values used by agent.call_model() today.
# agent.py is not modified in Phase 7, so this duplication is deliberate and
# is guarded by a regression check (phase7_selfcheck.py) that compares the
# captured payload of agent.call_model() with build_payload() output.
# ---------------------------------------------------------------------------

CHATML_SYSTEM_PREFIX = "<|im_start|>system\n"
CHATML_USER_PREFIX = "<|im_start|>user\n"
CHATML_ASSISTANT_PREFIX = "<|im_start|>assistant\n"
CHATML_SUFFIX = "<|im_end|>\n"
CHATML_STOP = "<|im_end|>"
NO_THINK_SUFFIX = "\n\n/no_think"

DEFAULT_N_PREDICT = 1600
DEFAULT_TEMPERATURE = 0.1
DEFAULT_REPEAT_PENALTY = 1.1
DEFAULT_TIMEOUT_SECONDS = 300


@dataclass(frozen=True)
class LLMProviderSpec:
    """
    Declarative description of an LLM provider.

    Attributes:
        id (str): Internal identifier (ASCII, stable, used as a registry key).
        label (str): Human-readable description for logs/UI.
        style (LLMStyle): Wire format of the provider.
        base_url (str): Scheme + host + optional path prefix. May be empty,
            in which case `base_url_env` must supply it.
        path (str): Endpoint path appended to base_url.
        model (Optional[str]): Default model name (None = provider decides).
        api_key_env (Optional[str]): Environment variable holding the API key.
            None means "no key required" (local server).
        base_url_env (Optional[str]): Environment variable that can override
            base_url at runtime.
        is_local (bool): True if the provider runs on this machine.
        supports_streaming (bool): Whether the provider supports streaming.
            The current runtime always sends stream=False.
        notes (str): Free-form integration note.
    """

    id: str
    label: str
    style: LLMStyle
    base_url: str
    path: str
    model: Optional[str] = None
    api_key_env: Optional[str] = None
    base_url_env: Optional[str] = None
    is_local: bool = False
    supports_streaming: bool = True
    notes: str = ""

    @property
    def requires_api_key(self) -> bool:
        """True if this provider needs an API key to be reachable."""
        return self.api_key_env is not None

    @property
    def uses_raw_prompt(self) -> bool:
        """True if the provider takes a single raw prompt (llama.cpp style)."""
        return self.style == LLMStyle.LLAMA_COMPLETION

    @property
    def endpoint_url(self) -> str:
        """Return base_url + path (static part only, no env override)."""
        return self.base_url.rstrip("/") + self.path

    def to_dict(self) -> Dict[str, object]:
        """Return a JSON-serialisable description (safe: never includes keys)."""
        return {
            "id": self.id,
            "label": self.label,
            "style": self.style.value,
            "endpoint": self.endpoint_url,
            "model": self.model,
            "requires_api_key": self.requires_api_key,
            "api_key_env": self.api_key_env,
            "base_url_env": self.base_url_env,
            "is_local": self.is_local,
            "supports_streaming": self.supports_streaming,
            "notes": self.notes,
        }

    def describe(self) -> str:
        """Return a short one-line description for logs."""
        endpoint = (
            self.endpoint_url if self.base_url else f"(unset){self.path}"
        )
        return (
            f"{self.id}: style={self.style.value}, "
            f"endpoint={endpoint}, "
            f"model={self.model or '-'}, "
            f"key={'required' if self.requires_api_key else 'not_required'}, "
            f"local={'yes' if self.is_local else 'no'}"
        )

# The current provider is the default. Its endpoint must stay identical to
# agent.MODEL_URL ("http://127.0.0.1:8080/completion").
DEFAULT_PROVIDER_ID = "llama_cpp_local"

PROVIDER_REGISTRY: Dict[str, LLMProviderSpec] = {
    "llama_cpp_local": LLMProviderSpec(
        id="llama_cpp_local",
        label="llama.cpp local server (current provider)",
        style=LLMStyle.LLAMA_COMPLETION,
        base_url="http://127.0.0.1:8080",
        path="/completion",
        model="Qwen3-8B",
        api_key_env=None,
        base_url_env="LOCAL_AGENT_LLAMA_BASE_URL",
        is_local=True,
        supports_streaming=True,
        notes=(
            "Default provider. Matches agent.MODEL_URL exactly and uses the "
            "same request body as agent.call_model()."
        ),
    ),
    "openrouter": LLMProviderSpec(
        id="openrouter",
        label="OpenRouter (OpenAI-compatible gateway)",
        style=LLMStyle.OPENAI_CHAT,
        base_url="https://openrouter.ai/api/v1",
        path="/chat/completions",
        model=None,
        api_key_env="OPENROUTER_API_KEY",
        base_url_env="OPENROUTER_BASE_URL",
        is_local=False,
        supports_streaming=True,
        notes="Future. Verify endpoint/model IDs before activation.",
    ),
    "agentrouter": LLMProviderSpec(
        id="agentrouter",
        label="AgentRouter (OpenAI-compatible gateway)",
        style=LLMStyle.OPENAI_CHAT,
        base_url="https://agentrouter.org/v1",
        path="/chat/completions",
        model=None,
        api_key_env="AGENTROUTER_API_KEY",
        base_url_env="AGENTROUTER_BASE_URL",
        is_local=False,
        supports_streaming=True,
        notes="Future. Verify endpoint/model IDs before activation.",
    ),
    "openai_compatible": LLMProviderSpec(
        id="openai_compatible",
        label="Generic OpenAI-compatible endpoint (self-hosted or cloud)",
        style=LLMStyle.OPENAI_CHAT,
        base_url="",
        path="/chat/completions",
        model=None,
        api_key_env="LOCAL_AGENT_LLM_API_KEY",
        base_url_env="LOCAL_AGENT_LLM_BASE_URL",
        is_local=False,
        supports_streaming=True,
        notes="Placeholder: base URL must come from the env variable.",
    ),
}


def get_provider(provider_id: str) -> LLMProviderSpec:
    """
    Return the provider spec for an id.

    Raises:
        KeyError: if the provider id is unknown.
    """
    key = (provider_id or "").strip()
    if key not in PROVIDER_REGISTRY:
        known = ", ".join(sorted(PROVIDER_REGISTRY))
        raise KeyError(f"Unknown LLM provider '{provider_id}'. Known: {known}")
    return PROVIDER_REGISTRY[key]


def default_provider() -> LLMProviderSpec:
    """Return the current default provider (llama.cpp local)."""
    return PROVIDER_REGISTRY[DEFAULT_PROVIDER_ID]


def list_providers() -> List[str]:
    """Return all registered provider ids, default first."""
    others = sorted(
        pid for pid in PROVIDER_REGISTRY if pid != DEFAULT_PROVIDER_ID
    )
    return [DEFAULT_PROVIDER_ID] + others


def resolve_endpoint(
    spec: LLMProviderSpec,
    env: Optional[Dict[str, str]] = None,
) -> str:
    """
    Resolve the full endpoint URL for a provider.

    Priority: environment override (spec.base_url_env) > spec.base_url.

    Args:
        spec: The provider spec.
        env: Optional environment mapping (defaults to os.environ).

    Returns:
        Full endpoint URL.

    Raises:
        ValueError: if no base URL is configured at all.
    """
    import os

    if env is None:
        env = os.environ

    base_url = spec.base_url
    if spec.base_url_env:
        override = (env.get(spec.base_url_env) or "").strip()
        if override:
            base_url = override

    if not base_url:
        raise ValueError(
            f"Provider '{spec.id}' has no base URL. "
            f"Set {spec.base_url_env or 'base_url'} explicitly."
        )

    return base_url.rstrip("/") + spec.path


def api_key_for(
    spec: LLMProviderSpec,
    env: Optional[Dict[str, str]] = None,
) -> Optional[str]:
    """
    Return the API key for a provider, read from its env variable.

    Returns None when no key is required or when the variable is not set.
    The value is never logged or stored by this module.
    """
    import os

    if env is None:
        env = os.environ

    if not spec.api_key_env:
        return None

    key = (env.get(spec.api_key_env) or "").strip()
    return key or None


def is_available(
    spec: LLMProviderSpec,
    env: Optional[Dict[str, str]] = None,
) -> bool:
    """
    Check whether a provider could be used right now — configuration only.

    This performs NO network call. It only checks that:
    - a base URL is configured (spec or env), and
    - an API key is present when the provider requires one.
    """
    try:
        resolve_endpoint(spec, env)
    except ValueError:
        return False

    if spec.requires_api_key and api_key_for(spec, env) is None:
        return False

    return True


def render_prompt(
    spec: LLMProviderSpec,
    system_prompt: str,
    user_prompt: str,
    no_think: bool = False,
) -> str:
    """
    Build the raw prompt string for raw-prompt providers (llama.cpp style).

    This reproduces the framing used by agent.call_model() today:

        <|im_start|>system\\n{system}<|im_end|>\\n
        <|im_start|>user\\n{user}<|im_end|>\\n
        <|im_start|>assistant\\n

    Args:
        spec: The provider spec (must use LLMStyle.LLAMA_COMPLETION).
        system_prompt: The agent's own system prompt (not modified).
        user_prompt: The task/context text.
        no_think: When True, appends the "/no_think" marker exactly like
            agent.call_model() does.

    Raises:
        ValueError: if the provider is not a raw-prompt provider.
    """
    if not spec.uses_raw_prompt:
        raise ValueError(
            f"Provider '{spec.id}' uses style '{spec.style.value}'; "
            "raw prompt rendering is only valid for llama_completion."
        )

    if no_think:
        user_prompt = user_prompt + NO_THINK_SUFFIX

    return (
        CHATML_SYSTEM_PREFIX
        + system_prompt
        + CHATML_SUFFIX
        + CHATML_USER_PREFIX
        + user_prompt
        + CHATML_SUFFIX
        + CHATML_ASSISTANT_PREFIX
    )


def build_payload(
    spec: LLMProviderSpec,
    system_prompt: str,
    user_prompt: str,
    n_predict: Optional[int] = None,
    no_think: bool = False,
    stream: bool = False,
) -> Dict[str, object]:
    """
    Build the request body for a provider.

    LLAMA_COMPLETION produces exactly the body used by agent.call_model()
    today (same keys, same values, same defaults) so the current runtime can
    be migrated onto this builder later without any behavioural change.

    OPENAI_CHAT produces a `messages` body for future providers.

    Args:
        spec: The provider spec.
        system_prompt: System instruction (kept unchanged).
        user_prompt: User/task text.
        n_predict: Optional generation cap. Defaults to 1600, like today.
        no_think: Appends "/no_think" for raw-prompt providers.
        stream: Always False in the current runtime.

    Returns:
        Dict ready to be json.dumps()-ed.
    """
    token_limit = (
        n_predict
        if isinstance(n_predict, int) and n_predict > 0
        else DEFAULT_N_PREDICT
    )

    if spec.uses_raw_prompt:
        return {
            "prompt": render_prompt(
                spec,
                system_prompt,
                user_prompt,
                no_think=no_think,
            ),
            "n_predict": token_limit,
            "temperature": DEFAULT_TEMPERATURE,
            "repeat_penalty": DEFAULT_REPEAT_PENALTY,
            "stop": [CHATML_STOP],
            "stream": stream,
        }

    return {
        "model": spec.model or "",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": DEFAULT_TEMPERATURE,
        "max_tokens": token_limit,
        "stream": stream,
    }


def build_headers(
    spec: LLMProviderSpec,
    env: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """
    Build the HTTP headers for a provider.

    Always includes Content-Type. Adds an Authorization header only when the
    provider requires a key AND that key is present in the environment.

    Raises:
        ValueError: if the provider requires a key and it is missing
            (fail closed — never send a request without its key).
    """
    headers = {"Content-Type": "application/json"}

    if not spec.requires_api_key:
        return headers

    key = api_key_for(spec, env)
    if key is None:
        raise ValueError(
            f"Provider '{spec.id}' requires an API key in "
            f"{spec.api_key_env}, but it is not set."
        )

    headers["Authorization"] = f"Bearer {key}"
    return headers


def describe_providers() -> str:
    """Return a multi-line description of all registered providers."""
    lines = ["LLM providers (registered):"]
    for provider_id in list_providers():
        spec = PROVIDER_REGISTRY[provider_id]
        marker = " [default]" if provider_id == DEFAULT_PROVIDER_ID else ""
        lines.append(f"- {spec.describe()}{marker}")
    return "\n".join(lines)
