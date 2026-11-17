"""ninklang.tech 公开牌谱中转服务客户端。

该服务由开源项目 ninkjin/Majsoul-Mortal-Analyzer 使用：
浏览器公开页需要验证码，但桌面匿名接口（/api/v1/desktop/requests）不需要。
流程：
  1. POST /api/v1/desktop/requests  {"share_url": ...}
     -> request_id + request_token (rq_...)
  2. GET  /api/v1/requests/<id>          Authorization: Request <token>
     直到 status == ready
  3. GET  /api/v1/requests/<id>/result   -> Tenhou 6 JSON

服务只负责“代取 + 转换”，不会上传本地数据；返回结果保留 7 天。
"""

from __future__ import annotations

import gzip
import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import config

MAX_METADATA = 2 * 1024 * 1024
MAX_RESULT = 64 * 1024 * 1024
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class RelayError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False, retry_after: float | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


class RelayResultNotReady(RelayError):
    """结果已排队但还没生成；按 Retry-After 继续等，不算失败。"""


class RelayUpstreamLimited(RelayError):
    """雀魂侧返回 540（牌谱拉取次数限制）；应停止整批，不要继续提交。"""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _request_json(
    url: str,
    *,
    method: str = "GET",
    body: dict | None = None,
    token: str | None = None,
    timeout: float = config.REQUEST_TIMEOUT,
    max_bytes: int = MAX_METADATA,
) -> dict:
    headers = {
        "Accept": "application/json",
        "User-Agent": "Mortal-paipu-analyzer/2.0 (paipu-db)",
        # 避免服务端 keep-alive 且无 Content-Length 时 read() 一直等
        "Connection": "close",
    }
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Request {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            raw = resp.read(max_bytes + 1)
            if resp.headers.get("Content-Encoding", "").lower() == "gzip" or raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            if len(raw) > max_bytes:
                raise RelayError("中转服务返回的数据过大")
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read(MAX_METADATA).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, OSError):
            payload = {}
        message = _error_message(exc.code, payload)
        retry_after = _retry_after(exc.headers)
        error = payload.get("error") if isinstance(payload, dict) else None
        error_code = str(error.get("code") or "") if isinstance(error, dict) else ""
        if error_code == "RESULT_NOT_READY":
            raise RelayResultNotReady(
                message, retryable=True, retry_after=retry_after or 2.0
            ) from None
        if error_code == "FETCH_PROTECTION" or "540" in message or "拉取限制" in message:
            raise RelayUpstreamLimited(message) from None
        if exc.code == 429 and retry_after is None:
            retry_after = config.RELAY_HTTP_RETRY_DELAY
        raise RelayError(message, retryable=exc.code in RETRYABLE_STATUS, retry_after=retry_after) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RelayError(f"无法连接中转服务：{exc}", retryable=True) from None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RelayError("中转服务返回的不是合法 JSON", retryable=True) from exc
    if not isinstance(payload, dict):
        raise RelayError("中转服务返回格式异常")
    return payload


def _retry_after(headers) -> float | None:
    try:
        value = float(headers.get("Retry-After", ""))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return min(value, 60.0)


def _error_message(status: int, payload: dict) -> str:
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        code = str(error.get("code") or "").strip()
        message = str(error.get("message") or "").strip()
        if code and message:
            return f"中转服务错误 {code}：{message}（HTTP {status}）"
        if code:
            return f"中转服务错误 {code}（HTTP {status}）"
        if message:
            return f"中转服务错误：{message}（HTTP {status}）"
    return f"中转服务请求失败（HTTP {status}）"


def _with_retries(fn, *, what: str, deadline: float):
    last: RelayError | None = None
    for attempt in range(config.RELAY_HTTP_RETRIES + 1):
        if time.monotonic() >= deadline:
            raise RelayError(f"{what}超时", retryable=True)
        try:
            return fn()
        except RelayError as exc:
            last = exc
            if not exc.retryable or attempt >= config.RELAY_HTTP_RETRIES:
                raise
            delay = exc.retry_after if exc.retry_after is not None else config.RELAY_HTTP_RETRY_DELAY * (attempt + 1)
            print(f"    {what}失败，{delay:.0f}s 后重试：{exc}", flush=True)
            time.sleep(min(delay, max(0.1, deadline - time.monotonic())))
    raise last or RelayError(f"{what}失败", retryable=True)


def service_info() -> dict:
    return _request_json(config.RELAY_BASE + "/api/v1/public/service-info")


def fetch_tenhou(
    share_url: str,
    out_path: Path,
    *,
    wait_seconds: float = config.RELAY_WAIT_SECONDS,
    poll_interval: float = config.RELAY_POLL_INTERVAL,
    force: bool = False,
    log=print,
) -> dict:
    """从中转服务取一份 Tenhou 6 JSON 并原子写入 out_path。

    返回解析后的 JSON；失败抛 RelayError。默认已存在 out_path 时不覆盖。
    """
    if not force and out_path.exists() and out_path.stat().st_size > 0:
        return json.loads(out_path.read_text(encoding="utf-8"))

    deadline = time.monotonic() + max(30.0, float(wait_seconds))
    create_url = config.RELAY_BASE + config.RELAY_CREATE_PATH

    payload = _with_retries(
        lambda: _request_json(create_url, method="POST", body={"share_url": share_url}),
        what="创建牌谱任务",
        deadline=deadline,
    )
    request_id = str(payload.get("request_id") or "").strip()
    token = str(payload.get("request_token") or "").strip()
    if not request_id or not token.startswith("rq_"):
        raise RelayError("中转服务没有返回有效的 request_id / request_token")
    status = str(payload.get("status") or "").strip()
    log(f"    任务 {request_id} 已创建，状态：{status}")

    while status != "ready":
        if status in ("failed", "expired"):
            error = payload.get("error")
            if isinstance(error, dict):
                code = str(error.get("code") or "")
                message = str(error.get("message") or "")
                text = f"中转服务任务失败：{code} {message}".strip()
                if code == "UPSTREAM_RATE_LIMITED" or "540" in message:
                    raise RelayUpstreamLimited(text)
                raise RelayError(text)
            raise RelayError(f"中转服务任务失败（{status}）")
        if status not in ("queued", "fetching", "retry_wait", "converting", ""):
            raise RelayError(f"中转服务返回未知状态：{status}")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RelayError(f"等待中转服务超时（task={request_id}）", retryable=True)
        try:
            poll_ms = int(payload.get("poll_after_ms") or 1000)
        except (TypeError, ValueError):
            poll_ms = 1000
        time.sleep(min(max(poll_interval, poll_ms / 1000.0), 5.0, remaining))
        status_url = config.RELAY_BASE + config.RELAY_STATUS_PATH.format(
            request_id=urllib.parse.quote(request_id, safe="")
        )
        payload = _with_retries(
            lambda: _request_json(status_url, token=token),
            what="查询牌谱任务",
            deadline=deadline,
        )
        status = str(payload.get("status") or "").strip()
        if status:
            log(f"    状态：{status}")

    result_url = config.RELAY_BASE + config.RELAY_RESULT_PATH.format(
        request_id=urllib.parse.quote(request_id, safe="")
    )
    result_deadline = deadline + 300
    attempts = 0
    while True:
        if time.monotonic() > result_deadline:
            raise RelayError("等待牌谱结果超时", retryable=True)
        try:
            result = _request_json(
                result_url,
                token=token,
                timeout=max(30.0, config.REQUEST_TIMEOUT),
                max_bytes=MAX_RESULT,
            )
            break
        except RelayResultNotReady as exc:
            delay = exc.retry_after or 2.0
            log(f"    结果尚未生成，{delay:.0f}s 后重试")
            time.sleep(min(delay, max(0.1, result_deadline - time.monotonic())))
            continue
        except RelayError as exc:
            attempts += 1
            if not exc.retryable or attempts > config.RELAY_HTTP_RETRIES:
                raise
            delay = exc.retry_after or config.RELAY_HTTP_RETRY_DELAY * attempts
            log(f"    下载结果失败，{delay:.0f}s 后重试：{exc}")
            time.sleep(min(delay, max(0.1, result_deadline - time.monotonic())))

    _validate(result)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=1)
        os.replace(tmp, out_path)
    finally:
        if tmp.exists():
            tmp.unlink()
    return result


def _validate(result: dict) -> None:
    if not isinstance(result.get("log"), list) or not result["log"]:
        raise RelayError("中转服务返回的 Tenhou JSON 缺少 log")
    names = result.get("name")
    if not isinstance(names, list) or len(names) != 4:
        raise RelayError("中转服务返回的 Tenhou JSON 缺少四家 name")
    if "ver" not in result:
        raise RelayError("中转服务返回的 Tenhou JSON 缺少 ver")
    for i, kyoku in enumerate(result["log"]):
        if not isinstance(kyoku, list) or len(kyoku) < 17:
            raise RelayError(f"第 {i + 1} 局的 log 结构异常")
