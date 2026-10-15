"""Immutable short-translation options, shared by HTTP and durable preparation.

DeepSeek's documented thinking toggle is scoped to its official TLS endpoint
and verified model names. Other OpenAI-compatible providers get no guessed
provider extensions. Policy changes do not reopen old request fences.
"""

from dataclasses import dataclass
from urllib.parse import urlsplit
import re

TRANSLATION_MAX_TOKENS = 200
PARAMETER_POLICY_VERSION = "short_translation_v1"
DEEPSEEK_MODELS = frozenset(("deepseek-flash", "deepseek-v4-flash",
                           "deepseek-v4-pro", "deepseek-v4-flash-vision-exp"))
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class RequestParameters:
    max_tokens: int = TRANSLATION_MAX_TOKENS
    thinking: str | None = None

    def __post_init__(self):
        if type(self.max_tokens) is not int or not 1 <= self.max_tokens <= 1024:
            raise ValueError("Invalid short-translation output limit")
        if self.thinking not in (None, "disabled"):
            raise ValueError("Unsupported short-translation thinking mode")

    def options(self):
        result = {"max_tokens": self.max_tokens, "temperature": 0, "stream": False}
        if self.thinking is not None:
            result["thinking"] = {"type": self.thinking}
        return result

    def snapshot(self):
        return dict(self.options(), policy_version=PARAMETER_POLICY_VERSION)


def request_parameters(endpoint, model, max_tokens=TRANSLATION_MAX_TOKENS):
    try:
        uri = urlsplit(endpoint)
        official = (uri.scheme == "https" and uri.hostname == "api.deepseek.com"
                    and uri.port in (None, 443) and uri.path in ("/chat/completions", "/v1/chat/completions")
                    and uri.username is None and uri.password is None and not uri.query and not uri.fragment)
    except (TypeError, ValueError, AttributeError):
        official = False
    return RequestParameters(max_tokens, "disabled" if official and model in DEEPSEEK_MODELS else None)


def validate_parameter_snapshot(params):
    """Accept only this policy's non-sensitive fields in new journal snapshots."""
    required = {"policy_version", "model", "endpoint_fingerprint", "prompt_fingerprint",
                "max_tokens", "temperature", "stream"}
    if (not isinstance(params, dict) or not required <= params.keys()
            or params.keys() - required - {"thinking"}
            or params["policy_version"] != PARAMETER_POLICY_VERSION):
        raise ValueError("Unsupported parameter snapshot")
    if (not isinstance(params["model"], str) or not 1 <= len(params["model"]) <= 200
            or any(not isinstance(params[name], str) or not _DIGEST.fullmatch(params[name])
                   for name in ("endpoint_fingerprint", "prompt_fingerprint"))):
        raise ValueError("Invalid parameter identity")
    thinking = params.get("thinking")
    if "thinking" in params and thinking != {"type": "disabled"}:
        raise ValueError("Invalid thinking snapshot")
    options = RequestParameters(params["max_tokens"], "disabled" if thinking is not None else None).options()
    if any(type(params[key]) is not type(value) or params[key] != value for key, value in options.items()):
        raise ValueError("Invalid transport snapshot")
    return {key: params[key] for key in required - options.keys()} | options
