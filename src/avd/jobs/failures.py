"""Classify persisted or live failures without exposing provider response bodies.

Isolated workers return tracebacks rather than SDK exception objects. Keep this
parser independent of provider SDKs so the API can classify historical failures
with the same policy used by the queue worker.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import TypedDict


class FailureInfo(TypedDict):
    code: str
    message: str
    retryable: bool
    retry_at: str | None
    http_status: int | None


_HARD_QUOTA = re.compile(
    r"\b(?:quota_exhausted|insufficient_quota|billing_hard_limit_reached|"
    r"billing_not_active|credit_balance_too_low)\b|"
    r"individual\s+quota\s+reached|exceeded\s+your\s+current\s+quota|"
    r"credit\s+balance\s+is\s+too\s+low|insufficient\s+(?:credits|credit\s+balance)",
    re.IGNORECASE,
)
_HTTP_STATUS = re.compile(
    r"(?:error\s+code|status\s+code|status_code|http(?:/\d(?:\.\d)?)?)"
    r"[\s:=]+([45]\d{2})\b|"
    r"[\"'](?:http_status|status_code|code)[\"']\s*:\s*([45]\d{2})\b",
    re.IGNORECASE,
)
_RESET_AT = re.compile(
    r"(?:quotaResetTimeStamp|quotaResetTimestamp|quota_reset_timestamp|retry_at|reset_at)"
    r"[\"']?\s*[:=]\s*[\"']([^\"'\s,}]+)",
    re.IGNORECASE,
)
_TRANSIENT_RATE = re.compile(
    r"\b(?:rate_limit_error|rate_limit_exceeded|ratelimiterror|rate_limited)\b|"
    r"rate\s+limit|too\s+many\s+requests",
    re.IGNORECASE,
)
_EXECUTION_NOT_STOPPED = re.compile(
    r"\bProcessCleanupError\b|"
    r"Process cleanup failed;\s*manual recovery required|"
    r"Remote ComfyUI execution was not verified stopped|"
    r"Managed worker cleanup uncertain;\s*manual recovery required|"
    r"Job release verification failed;\s*manual recovery required",
    re.IGNORECASE,
)


def _text(error: str | Exception) -> str:
    if isinstance(error, str):
        return error
    text = f"{type(error).__name__}: {error}"
    body = getattr(error, "body", None)
    if isinstance(body, (dict, list)):
        text += "\n" + json.dumps(body, ensure_ascii=False, default=str)
    return text


def _status(error: str | Exception, text: str) -> int | None:
    if isinstance(error, Exception):
        response = getattr(error, "response", None)
        for value in (getattr(error, "status_code", None), getattr(response, "status_code", None)):
            if isinstance(value, int) and not isinstance(value, bool) and 400 <= value <= 599:
                return value
    matches = list(_HTTP_STATUS.finditer(text))
    if matches:
        match = matches[-1]
        return int(match[1] or match[2])
    return None


def _retry_at(text: str) -> str | None:
    for match in reversed(list(_RESET_AT.finditer(text))):
        try:
            parsed = datetime.fromisoformat(match[1].replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                continue
            return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        except ValueError:
            continue
    # Relative reset delays in an old traceback refer to the original failure,
    # not to the time the API happens to read it. Do not invent a new deadline.
    return None


def classify_failure(error: str | Exception) -> FailureInfo:
    """Stop exhausted or uncertain paid requests, preserving transient retries."""
    text = _text(error)
    status = _status(error, text)
    code = "job_failed"
    message = "任务执行失败，请查看错误详情。"
    retryable = True
    if _EXECUTION_NOT_STOPPED.search(text):
        code = "execution_not_stopped"
        message = "GPU 已隔离，请先确认远端任务或残留进程已停止，再按错误详情执行独占恢复。"
        retryable = False
    elif _HARD_QUOTA.search(text):
        code = "quota_exhausted"
        message = "模型服务的账号额度已用尽，请等待额度恢复或更换可用模型。"
        retryable = False
    elif "ImageGenerationRejectedError" in text:
        code = "moderation_blocked"
        message = "图像服务的安全系统拒绝了当前请求。请更换素材或修改内容后重新提交。"
        retryable = False
    elif "ImageGenerationUncertainError" in text:
        code = "image_outcome_unknown"
        message = "图像请求已提交，但上游未返回完整结果，已停止自动重试。请检查图像服务后手动重试。"
        retryable = False
    elif status == 429 or _TRANSIENT_RATE.search(text):
        code = "rate_limited"
        message = "模型服务暂时限流，请稍后重试。"
    elif status in {401, 403} or re.search(
        r"\b(?:authentication_failed|authentication_error|invalid_api_key)\b", text, re.IGNORECASE
    ):
        code = "authentication_failed"
        message = "模型服务拒绝了访问，请检查账号权限和 API 设置。"
        retryable = False
    elif status in {400, 422} or re.search(
        r"\b(?:invalid_request|invalid_prompt)\b", text, re.IGNORECASE
    ):
        code = "invalid_request"
        message = "模型服务不接受当前请求，请检查模型和接口设置。"
        retryable = False
    elif status in {408, 504, 522, 524}:
        code = "timeout"
        message = f"模型服务请求超时（HTTP {status}），请稍后重试。"
    elif status is not None:
        code = "http_error"
        message = "模型服务请求失败，请查看错误详情。"
        retryable = status in {408, 409} or status >= 500
    elif re.search(r"timeout|timed\s+out", text, re.IGNORECASE):
        code = "timeout"
        message = "任务请求超时，请稍后重试。"
    elif re.search(
        r"\b(?:connection_failed|connectionerror|connecterror|apiconnectionerror|"
        r"remoteprotocolerror)\b",
        text,
        re.IGNORECASE,
    ):
        code = "connection_failed"
        message = "暂时无法连接模型服务，请检查接口地址或稍后重试。"
    return {
        "code": code,
        "message": message,
        "retryable": retryable,
        "retry_at": None if code == "execution_not_stopped" else _retry_at(text),
        "http_status": status,
    }


__all__ = ["FailureInfo", "classify_failure"]
