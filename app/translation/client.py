"""Chat-completion transport with validated, structured responses.

Transport options come from the shared request policy, never inferred separately
from the durable preparation. The caller owns retries.
"""

from dataclasses import dataclass, field
import json
import urllib.error
import urllib.request
from app.translation.parameters import TRANSLATION_MAX_TOKENS, request_parameters


RETRY_STATUS = frozenset({409, 425, 429, 500, 502, 503})
# [!] 2026-10-10：408 / 504 从 RETRY_STATUS 里单独拎出来。
#     这两个码说的是「服务端/网关等超时了」，也就是**请求已经到达供应商**、
#     可能已经生成并计费。把它们当"可重试"就是拿钱赌，而且与本文件
#     102-104 行自己的原则（"urllib 无法证明失败请求没到达供应商"）自相矛盾。
#     处理成 outcome_unknown：不自动重发，走可审计的人工恢复路径。
UNKNOWN_STATUS = frozenset({408, 504})
RETRY_EXCEPTIONS = frozenset({
    "URLError", "TimeoutError", "timeout", "ConnectionError", "ConnectionResetError",
    "ConnectionAbortedError", "ConnectionRefusedError", "IncompleteRead",
    "RemoteDisconnected", "SSLError", "CertificateError"})
TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")
_FINISH_REASONS = {
    "length": "译文被截断（finish=length），请检查模型输出限额",
    "content_filter": "API 拒绝返回译文",
    "aborted": "接口生成已中断，译文不完整",
    "insufficient_system_resource": "接口资源不足，译文不完整",
    "tool_calls": "接口返回了工具调用，未取得完整译文",
}


@dataclass(frozen=True)
class ApiResponse:
    text: str | None = None
    reason: str = ""
    retryable: bool = False
    usage: dict = field(default_factory=dict)
    finish_reason: str = ""
    outcome_unknown: bool = False
    candidate_text: str = ''  # Retain incomplete replies for human review only.

    def legacy(self):
        return self.text, f"[{self.reason}]" if self.reason else "", self.retryable


def parse_response(data):
    if not isinstance(data, dict):
        return ApiResponse(reason="API 响应格式错误")
    raw_usage = data.get("usage")
    usage = {key: value for key, value in (raw_usage.items() if isinstance(raw_usage, dict) else ())
             if key in TOKEN_FIELDS and type(value) is int and value >= 0}
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return ApiResponse(reason="API 响应缺少有效译文", usage=usage)
    choice = choices[0]
    finish = str(choice.get("finish_reason") or "")
    known_finish = finish == "stop" or finish in _FINISH_REASONS
    finish = finish if known_finish else ""
    message = choice.get("message")
    if not isinstance(message, dict):
        return ApiResponse(reason="API 响应缺少有效消息", usage=usage, finish_reason=finish)
    content = message.get("content")
    if content is not None and not isinstance(content, str):
        return ApiResponse(reason="API 译文格式错误", usage=usage, finish_reason=finish)
    if finish in _FINISH_REASONS:
        return ApiResponse(reason=_FINISH_REASONS[finish], usage=usage, finish_reason=finish,
                           candidate_text=(content or '').strip())
    if message.get("refusal"):
        return ApiResponse(reason=_FINISH_REASONS["content_filter"], usage=usage, finish_reason=finish)
    if message.get("tool_calls"):
        return ApiResponse(reason=_FINISH_REASONS["tool_calls"], usage=usage, finish_reason=finish)
    if finish != "stop":
        return ApiResponse(reason="接口缺少有效结束状态，不能确认译文完整",
                           usage=usage, finish_reason=finish,candidate_text=(content or '').strip())
    text = (content or "").strip()
    if not text:
        return ApiResponse(reason="空译文 finish=stop",
                           usage=usage, finish_reason=finish)
    return ApiResponse(text=text, usage=usage, finish_reason=finish)


class ChatCompletionClient:
    def request(self, *, endpoint, api_key, model, text, system_prompt,
                timeout=12.0, max_tokens=TRANSLATION_MAX_TOKENS, retry_status=RETRY_STATUS,
                retry_exceptions=RETRY_EXCEPTIONS, parameters=None):
        try:
            expected = request_parameters(endpoint, model, max_tokens)
            if parameters is not None and parameters != expected:
                raise ValueError("Mismatched request parameter snapshot")
        except (ValueError, TypeError):
            return ApiResponse(reason="翻译请求参数无效，未发起接口请求")
        payload = {"model": model, "messages": [
            {"role": "system", "content": system_prompt}, {"role": "user", "content": text}],
            **expected.options()}
        # [!] 本地构造与网络发送必须分开。payload/Request 构造失败说明
        #     **一个字节都没发出去**，既没花钱也没发出；原来它和网络中断共用
        #     同一个 except，被标成 outcome_unknown —— 而那个状态没有自动出口，
        #     等于因为一个本地错误把这句话永久封了。
        try:
            request = urllib.request.Request(
                endpoint, data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"})
        except (TypeError, ValueError) as exc:
            return ApiResponse(reason=f"翻译请求构造失败（{type(exc).__name__}），未发起接口请求")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            unknown = exc.code in UNKNOWN_STATUS
            detail = ("；结果未知，未自动重发" if unknown
                      else "（请求本身有问题，已停止重试）" if exc.code == 400 else "")
            return ApiResponse(reason=f"翻译失败 HTTP {exc.code}{detail}",
                               retryable=(exc.code in retry_status and not unknown),
                               outcome_unknown=unknown)
        except Exception as exc:
            name = type(exc).__name__
            # urllib cannot prove that a failed transport never reached the
            # supplier. Retrying a timeout/reset can purchase the same task twice.
            return ApiResponse(reason=f"翻译失败 {name}；结果未知，未自动重发",
                               outcome_unknown=True)
        # [!] 收到了 HTTP 响应但正文不是 JSON：供应商**确实处理过**这个请求
        #     （否则不会回 200），可能已计费，而我们又拿不到 usage。
        #     所以仍按「结果未知」处理 —— 不自动重发。区别在于原因要说真话，
        #     不要冒充传输失败；而且现在它有 resolve() 这个人工出口，
        #     不再是永久死路。
        try:
            data = json.loads(raw)
        except ValueError as exc:
            return ApiResponse(
                reason=f"翻译接口返回的不是有效 JSON（{type(exc).__name__}）；"
                       "响应已到达，结果未知，未自动重发",
                outcome_unknown=True)
        return parse_response(data)
