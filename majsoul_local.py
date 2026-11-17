"""本地雀魂账号抓取（可选路径）。

依赖两个开源项目：
  * ms-api (MahjongRepository/mahjong_soul_api)：雀魂 WebSocket / protobuf 协议
  * tensoul (Equim-chan/tensoul 的 Python 移植)：雀魂牌谱 -> 天凤 tenhou.net/6 JSON

公开中转服务（relay）限流较严时，可以配置一个国服雀魂账号走本地直连。
账号密码只从 config.local.json 或环境变量读取，不写入数据库/日志。

注意：雀魂协议会随客户端更新；本模块按 2026-11 的 4.0.51 客户端实现。
如果登录返回 151，请更新 ms-api 或调整 MAJSOUL_PACKAGE_VERSION / RESOURCE_VERSION。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import warnings
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

import config

MS_HOST = "https://game.maj-soul.com"
ASSET_BASE = MS_HOST + "/assetbundles/ASTC/"
LOGIN_BEAT_CONTRACT = "DF2vkXCnfeXp4WoGrBGNcJBufZiMN3uP"
MAX_RECORD_BYTES = 32 * 1024 * 1024
MAX_VERSION_BUNDLE_BYTES = 4 * 1024 * 1024


class LocalFetchError(RuntimeError):
    pass


def credentials() -> tuple[str, str]:
    account = os.environ.get("MAJSOUL_ACCOUNT") or str(config.MAJSOUL_ACCOUNT or "")
    password = os.environ.get("MAJSOUL_PASSWORD") or str(config.MAJSOUL_PASSWORD or "")
    if not account or not password:
        raise LocalFetchError(
            "本地模式需要雀魂国服账号：请在 config.local.json 填写 "
            '{"MAJSOUL_ACCOUNT": "...", "MAJSOUL_PASSWORD": "..."} '
            "或设置环境变量 MAJSOUL_ACCOUNT / MAJSOUL_PASSWORD"
        )
    return account, password


def _http_json(url: str, params: dict | None = None) -> dict:
    if params:
        url += "?" + urllib.parse.urlencode(params)
    last: Exception | None = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                    "Accept": "application/json,text/plain,*/*",
                    "Accept-Encoding": "gzip",
                    "Connection": "close",
                },
            )
            with urllib.request.urlopen(req, timeout=30) as response:
                raw = response.read()
                if response.headers.get("Content-Encoding", "").lower() == "gzip" or raw[:2] == b"\x1f\x8b":
                    import gzip

                    raw = gzip.decompress(raw)
                return json.loads(raw.decode("utf-8-sig"))
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < 2:
                time.sleep(2.0 * (attempt + 1))
    raise LocalFetchError(f"请求雀魂接口失败：{url}（{last}）")


def _fetch_bytes(url: str, limit: int = MAX_VERSION_BUNDLE_BYTES) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept-Encoding": "gzip",
            "Connection": "close",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise LocalFetchError(f"资源包过大：{url}")
    return data


def _load_unity_bundle(data: bytes):
    try:
        import UnityPy
    except ImportError:
        raise LocalFetchError(
            "需要 UnityPy 读取雀魂资源版本："
            ".venv\\Scripts\\pip install UnityPy"
        ) from None
    if not data.startswith(b"UnityFS\0"):
        raise LocalFetchError("雀魂资源包格式不是 UnityFS")
    UnityPy.config.FALLBACK_UNITY_VERSION = "2022.3.0f1"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return UnityPy.load(data)


def _resource_version_from_bundles() -> str:
    """按官方资源清单读取当前 WebGL 资源版本（登录版本串要用它）。

    version.json 里的 0.11.x.w 是协议/配置版本，不是资源版本；混用会 151。
    """
    manifest = _fetch_bytes(ASSET_BASE + "bundle_info_so.majset")
    env = _load_unity_bundle(manifest)
    names: set[str] = set()
    for obj in env.objects:
        if obj.type.name != "MonoBehaviour":
            continue
        try:
            table = obj.read_typetree()
        except Exception:  # noqa: BLE001 - 不同 Unity 版本的 typetree 可能失败
            continue
        bundles = table.get("bundleInfos", [])
        for asset in table.get("assetInfos", []):
            path = str(asset.get("assetPath", "")).replace("\\", "/").lower()
            if path != "myassets/docs_version/version.json":
                continue
            index = asset.get("ownerBundleIndex")
            if isinstance(index, int) and 0 <= index < len(bundles):
                name = str(bundles[index].get("name", ""))
                if re.fullmatch(r"[A-Za-z0-9_-]{1,150}\.majset", name):
                    names.add(name)
    if len(names) != 1:
        raise LocalFetchError(f"无法唯一定位雀魂资源版本清单：{names}")

    bundle = _fetch_bytes(ASSET_BASE + next(iter(names)))
    env = _load_unity_bundle(bundle)
    versions: set[str] = set()
    for path, pointer in env.container.items():
        if path.replace("\\", "/").lower() != "myassets/docs_version/version.json":
            continue
        obj = pointer.read()
        text = obj.m_Script
        if not isinstance(text, str):
            continue
        try:
            doc = json.loads(text)
        except json.JSONDecodeError:
            continue
        if doc.get("platform") == "WebGL" and isinstance(doc.get("version"), str):
            versions.add(doc["version"])
    if len(versions) != 1:
        raise LocalFetchError(f"无法唯一读取雀魂资源版本：{versions}")
    return versions.pop()


def _discover_versions() -> tuple[str, str, str, list[dict]]:
    """返回 (package_version, resource_version, config_version, gateways)。"""
    html = urllib.request.urlopen(
        urllib.request.Request(MS_HOST + "/1/", headers={"User-Agent": "Mozilla/5.0"}),
        timeout=30,
    ).read().decode("utf-8", "replace")
    matches = set(re.findall(r"productVersion\s*:\s*[\"'](\d{1,3}(?:\.\d{1,5}){2,3})[\"']", html))
    package_version = str(config.MAJSOUL_PACKAGE_VERSION or "").strip()
    if not package_version:
        if len(matches) != 1:
            raise LocalFetchError("无法识别雀魂客户端版本；请在 config.local.json 填 MAJSOUL_PACKAGE_VERSION")
        package_version = matches.pop()

    legacy = _http_json(MS_HOST + "/1/version.json")
    config_version = str(legacy.get("version", "")).strip()
    if not re.fullmatch(r"[0-9.]+\.w", config_version):
        raise LocalFetchError("雀魂 version.json 格式已变化")

    resource_version = str(config.MAJSOUL_RESOURCE_VERSION or "").strip()
    if not resource_version:
        try:
            resource_version = _resource_version_from_bundles()
        except Exception as exc:  # noqa: BLE001
            fallback = config_version[:-2] if config_version.endswith(".w") else config_version
            print(
                f"WARN 无法从资源包读取版本（{exc}），退回 version.json 的 {fallback}；"
                "若登录返回 151，请在 config.local.json 填当前资源版本。",
                file=sys.stderr,
                flush=True,
            )
            resource_version = fallback
    print(
        f"雀魂客户端：package={package_version} resource={resource_version}",
        file=sys.stderr,
        flush=True,
    )

    conf = _http_json(MS_HOST + f"/1/v{config_version}/config.json")
    groups = conf.get("ip") or []
    if not groups or not isinstance(groups[0], dict):
        raise LocalFetchError("雀魂网关配置缺少 ip")
    gateways = groups[0].get("gateways") or groups[0].get("region_urls") or []
    return package_version, resource_version, config_version, gateways


def _routes(package_version: str, gateways: list[dict]) -> list[tuple[str, str]]:
    last_error: Exception | None = None
    for gateway in gateways[:5]:
        try:
            url = str(gateway["url"]).rstrip("/") + "/api/clientgate/routes"
            doc = _http_json(url, {"platform": "Web", "version": package_version, "lang": "chs_t"})
            rows = (doc.get("data") or {}).get("routes") or []
            result = []
            for row in rows:
                if row.get("ssl") is not True:
                    continue
                domain = str(row.get("domain") or "")
                route_id = str(row.get("id") or "")
                if domain and route_id:
                    result.append((f"wss://{domain}/gateway", route_id))
            if result:
                return result[:5]
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    raise LocalFetchError(f"无法获取雀魂网关线路：{last_error}")


async def _fetch(uuid: str, account: str, password: str, out_path: Path,
                 package_version: str, resource_version: str,
                 routes: list[tuple[str, str]]) -> dict:
    import aiohttp
    import ms.protocol_pb2 as pb
    import websockets
    from ms.base import MSRPCChannel
    from ms.rpc import Lobby, Route
    from tensoul import MajsoulPaipuDownloader

    class Channel(MSRPCChannel):
        async def connect(self, origin):
            self._ws = await websockets.connect(
                self._endpoint, origin=origin, open_timeout=15, close_timeout=5,
                max_size=MAX_RECORD_BYTES,
            )
            self._msg_dispatcher = asyncio.create_task(self.dispatch_msg())

    channel = None
    heartbeat = None
    try:
        last_error = None
        lobby = None
        route = None
        for endpoint, route_id in routes:
            channel = Channel(endpoint)
            try:
                await asyncio.wait_for(channel.connect(MS_HOST), timeout=20)
                route = Route(channel)
                req = pb.ReqRequestConnection(
                    type=1, route_id=route_id, timestamp=int(time.time()), platform="Web"
                )
                res = await asyncio.wait_for(route.request_connection(req), timeout=20)
                if res.result != 1:
                    raise LocalFetchError(f"网关握手失败：result={res.result}")
                lobby = Lobby(channel)
                break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                with suppress(asyncio.CancelledError, Exception):
                    await channel.close()
                channel = None
        if lobby is None or channel is None:
            raise LocalFetchError(f"无法连接雀魂网关：{last_error}")

        async def keepalive():
            while True:
                await asyncio.sleep(15)
                with suppress(Exception):
                    await asyncio.wait_for(route.heartbeat(pb.ReqHeartbeat(platform=11)), timeout=10)

        heartbeat = asyncio.create_task(keepalive())

        client_version = f"WebGL_2022-{resource_version}"
        login = pb.ReqLogin(
            account=account,
            password=hmac.new(b"lailai", password.encode("utf-8"), hashlib.sha256).hexdigest(),
            random_key=str(uuid4()),
            gen_access_token=True,
            reconnect=False,
            client_version_string=client_version,
            currency_platforms=[1, 2, 5, 6, 8, 10, 11],
            type=0,
            tag="cn",
        )
        login.device.CopyFrom(pb.ClientDeviceInfo(
            platform="pc", hardware="pc", os="windows", os_version="win10",
            is_browser=True, software="Chrome", sale_platform="web", screen_type=1,
        ))
        login.client_version.package = package_version
        login.client_version.resource = resource_version
        res = await asyncio.wait_for(lobby.login(login), timeout=30)
        if res.HasField("error") and res.error.code:
            raise LocalFetchError(f"雀魂登录失败，错误码 {res.error.code}")
        if not res.account_id and not res.access_token:
            raise LocalFetchError("雀魂登录未返回有效结果")

        beat = await asyncio.wait_for(
            lobby.login_beat(pb.ReqLoginBeat(contract=LOGIN_BEAT_CONTRACT)), timeout=20
        )
        if beat.HasField("error") and beat.error.code:
            raise LocalFetchError(f"雀魂 loginBeat 失败，错误码 {beat.error.code}")
        await asyncio.wait_for(lobby.login_success(pb.ReqCommon()), timeout=20)

        record = await asyncio.wait_for(
            lobby.fetch_game_record(pb.ReqGameRecord(
                game_uuid=uuid, client_version_string=client_version,
            )),
            timeout=60,
        )
        if record.HasField("error") and record.error.code:
            raise LocalFetchError(f"雀魂返回牌谱错误，错误码 {record.error.code}")
        if not record.data and record.data_url:
            req = urllib.request.Request(
                record.data_url,
                headers={"User-Agent": "Mozilla/5.0", "Connection": "close"},
            )
            with urllib.request.urlopen(req, timeout=60) as response:
                record.data = response.read(MAX_RECORD_BYTES + 1)
        if not record.data:
            raise LocalFetchError("雀魂没有返回牌谱数据")

        result = MajsoulPaipuDownloader()._handle_game_record(record)
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
    finally:
        if heartbeat is not None:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await heartbeat
        if channel is not None:
            with suppress(asyncio.CancelledError, Exception):
                await channel.close()


def fetch_tenhou(uuid: str, out_path: Path, *, force: bool = False) -> dict:
    """用本地雀魂账号抓取并转换为天凤 JSON。"""
    if not force and out_path.exists() and out_path.stat().st_size > 0:
        return json.loads(out_path.read_text(encoding="utf-8"))
    account, password = credentials()
    package_version, resource_version, _config_version, gateways = _discover_versions()
    routes = _routes(package_version, gateways)
    return asyncio.run(_fetch(
        uuid, account, password, out_path, package_version, resource_version, routes
    ))


def convert_raw(raw_path: Path, out_path: Path) -> dict:
    """把保存下来的 ResGameRecord protobuf 转成天凤 JSON。"""
    import ms.protocol_pb2 as pb
    from tensoul import MajsoulPaipuDownloader

    record = pb.ResGameRecord()
    record.ParseFromString(raw_path.read_bytes())
    result = MajsoulPaipuDownloader()._handle_game_record(record)
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
