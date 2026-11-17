"""项目配置：路径、赛事、队伍、接口地址。

所有可调项都可以用项目根目录的 config.local.json 覆盖（该文件不入库）。
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
TENHOU_DIR = DATA_DIR / "tenhou_json"
RAW_DIR = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"
LEAGUE_CACHE_DIR = DATA_DIR / "league_cache"
LOG_DIR = DATA_DIR / "logs"
DB_PATH = DATA_DIR / "paipu.db"

# 大凤林（000.mk）赛事 ID 与目标队伍
CID = 220
TEAM_NAME = "立石"

# 大凤林数据接口
LEAGUE_API = "https://cdn.r-mj.com/api/data.php"

# ninklang.tech 公开牌谱中转服务（无需雀魂账号）。
# 客户端协议见 ninkjin/Majsoul-Mortal-Analyzer 的 remote_service_fetcher.py。
RELAY_BASE = "https://ninklang.tech"
RELAY_CREATE_PATH = "/api/v1/desktop/requests"
RELAY_STATUS_PATH = "/api/v1/requests/{request_id}"
RELAY_RESULT_PATH = "/api/v1/requests/{request_id}/result"

# 抓取参数
REQUEST_TIMEOUT = 40
RELAY_WAIT_SECONDS = 900
RELAY_POLL_INTERVAL = 1.5
# 公开中转服务按 IP 限流：连续请求会 429。默认放慢到 75 秒一次。
FETCH_DELAY_SECONDS = 75.0
RELAY_HTTP_RETRIES = 4
RELAY_HTTP_RETRY_DELAY = 90.0
# 大凤林接口
LEAGUE_HTTP_RETRIES = 4
LEAGUE_HTTP_RETRY_DELAY = 3.0

# 可选：本地雀魂账号（mode=local 时使用）。也可以放到 config.local.json。
MAJSOUL_ACCOUNT = ""
MAJSOUL_PASSWORD = ""
# local 模式：程序版本从网页 productVersion 读取；资源版本从
# assetbundles/ASTC 的 docs_version/version.json 读取（需要 UnityPy）。
# 留空则自动探测。注意 version.json 的 0.11.x.w 不是资源版本。
MAJSOUL_PACKAGE_VERSION = ""
MAJSOUL_RESOURCE_VERSION = ""


def _load_overrides() -> dict:
    path = ROOT / "config.local.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"config.local.json 读取失败：{exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit("config.local.json 必须是 JSON 对象")
    return data


_OVERRIDES = _load_overrides()

for _key, _value in _OVERRIDES.items():
    if _key.isupper() and _key in globals():
        globals()[_key] = _value

DATA_DIR.mkdir(parents=True, exist_ok=True)
TENHOU_DIR.mkdir(parents=True, exist_ok=True)
RAW_DIR.mkdir(parents=True, exist_ok=True)
PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
LEAGUE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)


def rel(path: Path | str | None) -> str:
    """把绝对路径变成项目内相对路径；空值返回空串。"""
    if not path:
        return ""
    p = Path(path)
    try:
        return p.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return p.as_posix()


def abs_path(value: str) -> Path:
    """把数据库里的相对路径还原成绝对路径。"""
    p = Path(value)
    return p if p.is_absolute() else ROOT / p
