"""大凤林（000.mk）赛事数据接口客户端。

接口（无需登录）：
  GET https://cdn.r-mj.com/api/data.php?t=admin&cid=<cid>
  GET https://cdn.r-mj.com/api/data.php?t=team&cid=<cid>
  GET https://cdn.r-mj.com/api/data.php?t=class&cid=<cid>
  GET https://cdn.r-mj.com/api/data.php?t=c_data&cid=<cid>&r=<round>

c_data 的结构是 {rid: [10 个半庄记录]}；每个半庄记录包含
name1..name4 / num1..num4 / pint1..pint4 / url / rowi / rid / round。
"""

from __future__ import annotations

import gzip
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

import config


class LeagueError(RuntimeError):
    pass


def _http_get(url: str) -> bytes:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) paipu-db/1.0",
        "Accept": "application/json,text/plain,*/*",
        "Accept-Encoding": "gzip",
    }
    last_error: Exception | None = None
    for attempt in range(config.LEAGUE_HTTP_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=config.REQUEST_TIMEOUT) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding", "").lower() == "gzip" or raw[:2] == b"\x1f\x8b":
                    raw = gzip.decompress(raw)
                return raw
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt >= config.LEAGUE_HTTP_RETRIES:
                break
            time.sleep(config.LEAGUE_HTTP_RETRY_DELAY * (attempt + 1))
    raise LeagueError(f"请求失败：{url}（{last_error}）") from last_error


def fetch_json(t: str, cid: int = config.CID, **params: object) -> object:
    query = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{config.LEAGUE_API}?t={t}&cid={cid}"
    if query:
        url += "&" + query
    raw = _http_get(url)
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LeagueError(f"接口返回的不是合法 JSON：{url}") from exc


def get_admin(cid: int = config.CID) -> dict:
    data = fetch_json("admin", cid)
    if not isinstance(data, dict):
        raise LeagueError(f"赛事 {cid} 不存在或接口异常")
    return data


def get_teams(cid: int = config.CID) -> dict[str, dict]:
    data = fetch_json("team", cid)
    if not isinstance(data, dict):
        raise LeagueError("team 接口返回格式异常")
    return {str(k): v for k, v in data.items()}


def get_classes(cid: int = config.CID) -> list[dict]:
    data = fetch_json("class", cid)
    if not isinstance(data, list):
        if isinstance(data, dict) and not data:
            return []
        raise LeagueError("class 接口返回格式异常")
    return data


def get_round(cid: int, round_no: int) -> dict[str, list[dict]]:
    """返回 {rid: [10 个半庄]}；未开始的轮次返回空字典。"""
    data = fetch_json("c_data", cid, r=round_no)
    if data in ([], {}, "", None):
        return {}
    if not isinstance(data, dict):
        raise LeagueError(f"第 {round_no} 轮 c_data 返回格式异常")
    return {str(k): v for k, v in data.items()}


_SIMPLIFIED = {"先鋒": "先锋", "次鋒": "次锋", "中堅": "中坚", "副將": "副将", "大將": "大将"}


def _normalize_position(name: str) -> str:
    return _SIMPLIFIED.get(name, name)


def parse_positions(admin: dict) -> list[str]:
    """把 t_type 的 10 行位置名解析成 rowi -> 位置名（统一为简体）。

    例：先锋, 先锋, 次锋, 次锋, 中坚, 中坚, 副将, 副将, 大将, 大将。
    """
    lines = [line.strip() for line in str(admin.get("t_type", "")).replace("\r", "").split("\n")]
    lines = [_normalize_position(line) for line in lines if line]
    if len(lines) >= 10:
        return lines[:10]
    if len(lines) == 5:
        return [name for name in lines for _ in range(2)]
    # 兜底
    return ["先锋", "先锋", "次锋", "次锋", "中坚", "中坚", "副将", "副将", "大将", "大将"]


def position_label(positions: list[str], rowi: int) -> tuple[str, int, str]:
    """返回 (位置名, 当日同位置序号, 展示名)。rowi=0 -> 先锋1。"""
    position = positions[rowi] if 0 <= rowi < len(positions) else f"第{rowi + 1}局"
    index = sum(
        1 for i in range(0, rowi + 1)
        if i < len(positions) and positions[i] == position
    )
    return position, index, f"{position}{index}"


def find_team_ids(teams: dict[str, dict], name: str) -> set[int]:
    wanted = str(name).strip()
    ids = set()
    for tid, team in teams.items():
        if str(team.get("t_name", "")).strip() == wanted:
            ids.add(int(tid))
    return ids


def team_name(teams: dict[str, dict], tid: int) -> str:
    team = teams.get(str(tid))
    return str(team.get("t_name", "")) if team else ""


def match_team_side(match: dict, target_tids: set[int]) -> int | None:
    """返回目标队伍在这场比赛中的序号 1..4；不参加返回 None。"""
    for i in range(1, 5):
        try:
            if int(match.get(f"tid{i}", 0)) in target_tids:
                return i
        except (TypeError, ValueError):
            continue
    return None


def game_date(entry: dict) -> str:
    """从分享链接里的 YYMMDD 前缀解析比赛日期；失败则退回 i_time。"""
    url = str(entry.get("url", ""))
    marker = "paipu="
    if marker in url:
        uuid = url.split(marker, 1)[1].split("&", 1)[0]
        prefix = uuid.split("-", 1)[0]
        if len(prefix) == 6 and prefix.isdigit():
            yy, mm, dd = int(prefix[:2]), int(prefix[2:4]), int(prefix[4:6])
            return f"20{yy:02d}-{mm:02d}-{dd:02d}"
    itime = str(entry.get("i_time", ""))
    return itime[:10] if len(itime) >= 10 else ""


def paipu_uuid(url: str) -> str:
    marker = "paipu="
    if marker not in url:
        return ""
    return url.split(marker, 1)[1].split("&", 1)[0]
