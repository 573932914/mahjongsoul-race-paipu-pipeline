"""ricochet.cn NAGA 投递客户端。

流程（与 ricochet NAGA 助手前端一致）：
  1. GET  /api/naga/get_account                     取主账号 id / left_np / 报告列表
  2. POST /api/naga/analyze_majsoul_step2            提交自定义天凤 JSON
     form: text=https://tenhou.net/6/#json=<compact JSON>
           player_types=2,4  (2=Nishiki 标准, 4=Kagashi 超副露)
           id=<NAGA account id>, mortal=0, ...
  3. 轮询 get_account，按四家姓名找到新报告
  4. 报告链接：
     https://ricochet.cn/api/naga/proxy/htmls/report_viewer.html?report_id=<id>&tw=0

Token 是 ricochet 登录凭证，等价于密码；只从 --token-file / RICOCHET_TOKEN /
data/ricochet_token.txt 读取，不写日志、不入库。
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

import config
import db

BASE = "https://ricochet.cn"
ANALYZE_PATH = "/api/naga/analyze_majsoul_step2"
ACCOUNT_PATH = "/api/naga/get_account"
DEFAULT_PLAYER_TYPES = "2,4"  # Nishiki + Kagashi
PLAYER_TYPE_NAMES = {
    0: "オメガ(副露)",
    1: "ガンマ(守备)",
    2: "ニシキ(标准)",
    3: "ヒバカリ(超门前)",
    4: "カガシ(超副露)",
    5: "アナンタ(超避四)",
    6: "ウロボロス(争一位)",
}


class NagaError(RuntimeError):
    pass


def load_token(path: str | Path | None = None) -> str:
    if path:
        p = Path(path)
        if not p.exists():
            raise NagaError(f"token 文件不存在：{p}")
        token = p.read_text(encoding="utf-8").strip().splitlines()[0].strip()
    else:
        token = os.environ.get("RICOCHET_TOKEN", "").strip()
        if not token:
            p = config.DATA_DIR / "ricochet_token.txt"
            if p.exists():
                token = p.read_text(encoding="utf-8").strip().splitlines()[0].strip()
    if not token:
        raise NagaError(
            "没有 ricochet token：用 --token-file、环境变量 RICOCHET_TOKEN，"
            "或把 token 放到 data/ricochet_token.txt"
        )
    return token


def _request(url: str, token: str, *, data: dict | None = None,
             timeout: float = 180) -> dict:
    headers = {
        "Authorization": "token " + token,
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json, text/plain, */*",
    }
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded;charset=UTF-8"
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except Exception as exc:  # noqa: BLE001
        detail = ""
        if hasattr(exc, "read"):
            try:
                detail = exc.read().decode("utf-8", "replace")[:500]
            except Exception:  # noqa: BLE001
                pass
        raise NagaError(f"ricochet 请求失败：{exc} {detail}".strip()) from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NagaError("ricochet 返回的不是合法 JSON") from exc


def get_account(token: str) -> dict:
    data = _request(BASE + ACCOUNT_PATH, token, timeout=30)
    if data.get("status") != 1:
        raise NagaError(f"获取 ricochet 账号失败：{data.get('msg')}")
    return data


def main_account(account_data: dict) -> dict:
    accounts = ((account_data.get("data") or {}).get("accounts") or [])
    if not accounts:
        raise NagaError("ricochet 账号没有绑定 NAGA 账户")
    for acc in accounts:
        if acc.get("is_main"):
            return acc
    return accounts[0]


def iter_reports(account_data: dict):
    for acc in ((account_data.get("data") or {}).get("accounts") or []):
        for entry in ((acc.get("reports") or {}).get("report") or []):
            yield acc, entry


def report_fields(entry: dict) -> tuple[str, set[str], str, str]:
    """返回 (raw_name, 四家姓名集合, report_id, player_types)。"""
    data = entry.get("data") or []
    raw_name = str(data[0]) if len(data) > 0 else ""
    players = set()
    if len(data) > 1 and isinstance(data[1], list):
        for item in data[1]:
            if isinstance(item, list) and item:
                players.add(str(item[0]))
    report_id = str(data[2]) if len(data) > 2 else ""
    ptypes = str((data[4] or [None, None, None, ""])[3]) if len(data) > 4 and isinstance(data[4], list) and len(data[4]) > 3 else ""
    return raw_name, players, report_id, ptypes


def find_report(account_data: dict, player_names: set[str],
                before_raw: set[str] | None = None,
                player_types: str = DEFAULT_PLAYER_TYPES) -> dict | None:
    """按四家姓名找报告；优先找 before_raw 之外的新报告。"""
    wanted = set(player_names)
    matches = []
    for _acc, entry in iter_reports(account_data):
        raw, players, rid, ptypes = report_fields(entry)
        if not rid or players != wanted:
            continue
        if player_types and ptypes and ptypes != player_types:
            continue
        matches.append((raw, entry))
    if not matches:
        # 姓名可能有截断/别名：退化为至少 3 人相同
        for _acc, entry in iter_reports(account_data):
            raw, players, rid, ptypes = report_fields(entry)
            if not rid or len(players & wanted) < 3:
                continue
            if player_types and ptypes and ptypes != player_types:
                continue
            matches.append((raw, entry))
    if not matches:
        return None
    if before_raw:
        # 严格只看新报告：X1/X2 四家姓名相同，不能用旧报告顶替
        matches = [x for x in matches if x[0] not in before_raw]
        if not matches:
            return None
    matches.sort(key=lambda x: x[0], reverse=True)
    return matches[0][1]


def find_existing_report(account_data: dict, player_names: set[str],
                         player_types: str = DEFAULT_PLAYER_TYPES,
                         report_id: str | None = None) -> dict | None:
    """在已有报告里找（用于批次中断后的补链接；X1/X2 同名时建议给 report_id）。"""
    wanted = set(player_names)
    matches = []
    for _acc, entry in iter_reports(account_data):
        raw, players, rid, ptypes = report_fields(entry)
        if not rid or players != wanted:
            continue
        if player_types and ptypes and ptypes != player_types:
            continue
        if report_id and rid != report_id:
            continue
        matches.append((raw, entry))
    if not matches:
        return None
    matches.sort(key=lambda x: x[0], reverse=True)
    return matches[0][1]


def report_urls(report_id: str) -> tuple[str, str]:
    proxy = (f"{BASE}/api/naga/proxy/htmls/report_viewer.html"
             f"?report_id={urllib.parse.quote(report_id, safe='')}&tw=0")
    direct = (f"https://naga.dmv.nico/htmls/report_viewer.html"
              f"?report_id={urllib.parse.quote(report_id, safe='')}&tw=0")
    return proxy, direct


def normalize_for_naga(data: dict, *, starts_mode: str = "all") -> dict:
    """投递 NAGA 前的兼容处理（不改原始/处理后文件）。

    starts_mode:
      - "shift" ：把整份牌谱整体平移到首局 100000，后续局仍按实际得失滚动。
                  减去的是 X1 累积得失，保留牌局内部点数流。**实测 NAGA 仍 405**。
      - "shift75"：整份牌谱每人减 75000，使首局和为 100000，同时保留
                  25000 + X1 得失的相对差。**已验证可用（默认/推荐）**：
                  #22 首局 [11700,14700,39100,34500] 成功，且保留 X1 累积得失。
      - "shift25"：首局平移到天凤标准 **25000**。**已验证可用**：
                  保留了局间实际得失滚动，但会丢掉 X1 累积得失。
      - "all"   ：每一局开局都改成 100000（忽略局间得失）。ricochet/NAGA 可用，
                  但 X2/大将 会失去 X1 累积得失的影响。
      - "first" ：只改首局（东1 0本场）。**实测 NAGA 仍返回 405**，仅留作研究。
      - "none"  ：不调整开局（严格按处理后 JSON；非均匀开局会被 NAGA 拒绝）。

    另外：四人全不听的流局 tensoul 输出 ["流局",[0,0,0,0]]，NAGA 用
    ["全員不聴",[0,0,0,0]] 的兼容形。
    """
    import copy

    if starts_mode not in ("shift", "shift25", "shift75", "all", "first", "none"):
        raise NagaError(f"未知 starts_mode：{starts_mode}")
    out = copy.deepcopy(data)
    log = out.get("log") or []
    if starts_mode == "shift75":
        offset = [-75000, -75000, -75000, -75000]
        for kyoku in log:
            if isinstance(kyoku, list) and len(kyoku) >= 2:
                kyoku[1] = [int(v) + offset[i] for i, v in enumerate(kyoku[1])]
    elif starts_mode in ("shift", "shift25") and log and isinstance(log[0], list) and len(log[0]) >= 2:
        base = 25000 if starts_mode == "shift25" else 100000
        first = [int(x) for x in log[0][1]]
        offset = [base - x for x in first]
        for kyoku in log:
            if isinstance(kyoku, list) and len(kyoku) >= 2:
                kyoku[1] = [int(v) + offset[i] for i, v in enumerate(kyoku[1])]
    elif starts_mode == "all":
        for kyoku in log:
            if isinstance(kyoku, list) and len(kyoku) >= 2:
                kyoku[1] = [100000, 100000, 100000, 100000]
    elif starts_mode == "first" and log and isinstance(log[0], list) and len(log[0]) >= 2:
        log[0][1] = [100000, 100000, 100000, 100000]
    for kyoku in log:
        if not isinstance(kyoku, list) or not kyoku:
            continue
        result = kyoku[-1]
        if (isinstance(result, list) and len(result) >= 2
                and result[0] == "流局"
                and isinstance(result[1], list)
                and all(int(x) == 0 for x in result[1])):
            kyoku[-1] = ["全員不聴", [0, 0, 0, 0]]
    return out


def editor_shape(obj: dict) -> dict:
    """天鳳 /6 エディタの Y() 形式：title/name/rule(aka)/log，去掉 ver/src。"""
    out = {k: obj[k] for k in ("title", "name") if k in obj}
    rule = dict(obj.get("rule") or {})
    aka = [rule.get("aka51"), rule.get("aka52"), rule.get("aka53")]
    if aka[0] is not None and aka[0] == aka[1] == aka[2]:
        for k in ("aka51", "aka52", "aka53"):
            rule.pop(k, None)
        rule["aka"] = aka[0]
    if not rule.get("disp"):
        rule.pop("disp", None)
    if rule:
        out["rule"] = rule
    out["log"] = obj["log"]
    return out


def per_round_payloads(data: dict, *, starts_mode: str = "none") -> list[dict]:
    """官方 NAGA custom API 的 json_data：每局一个编辑器格式 JSON。

    starts_mode 默认 "none"：保留处理后（X1/X2 规则）的开局点，不改成 100000。
    """
    normalized = normalize_for_naga(data, starts_mode=starts_mode)
    result = []
    for kyoku in normalized.get("log") or []:
        one = {k: v for k, v in normalized.items() if k != "log"}
        one["log"] = [kyoku]
        result.append(editor_shape(one))
    return result


def estimate_cost(rounds: int, player_types: str) -> int:
    """ricochet 前端对自定义牌谱的估价：max(2, 类型数) + 8 NP / 局。"""
    types = [x for x in str(player_types).split(",") if x.strip()]
    return int(rounds) * (max(2, len(types)) + 8)


def batch_round(
    round_no: int,
    *,
    token: str,
    player_types: str = "2,4,6",
    wait_seconds: float = 900,
    force: bool = False,
    limit: int | None = None,
    dry_run: bool = False,
    starts_mode: str = "shift75",
    verbose: bool = True,
) -> dict:
    """把某一轮所有未投递的半庄依次提交 NAGA，并回填 naga_url。"""
    conn = db.connect()
    errors: list[dict] = []
    submitted = 0
    try:
        games = list(conn.execute(
            "SELECT * FROM games WHERE cid=? AND round_no=? ORDER BY rowi",
            (config.CID, round_no),
        ))
        if not force:
            games = [g for g in games if not (g["naga_url"] or "").strip()]
        if limit:
            games = games[:limit]
        if not games:
            print(f"第 {round_no} 轮没有需要投递的半庄。")
            return {"submitted": 0, "errors": [], "cost": 0}

        rounds_total = 0
        usable = []
        for game in games:
            path_value = str(game["processed_json_path"] or "")
            path = config.abs_path(path_value) if path_value else None
            if not path or not path.exists():
                errors.append({"id": game["id"], "position": game["position_label"],
                               "error": "缺少 processed_json_path 或文件不存在"})
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                rounds = len(data.get("log") or [])
            except (OSError, json.JSONDecodeError) as exc:
                errors.append({"id": game["id"], "position": game["position_label"],
                               "error": f"读取 JSON 失败：{exc}"})
                continue
            usable.append((game, rounds))
            rounds_total += rounds

        estimated = estimate_cost(rounds_total, player_types)
        account = get_account(token)
        acc = main_account(account)
        left_np = acc.get("left_np")
        if verbose:
            print(f"第 {round_no} 轮：{len(usable)} 个半庄，{rounds_total} 局，"
                  f"NAGA 类型 {player_types}，预计 {estimated} NP，"
                  f"账户 {acc.get('id')} 剩余 {left_np} NP", flush=True)
            for game, rounds in usable:
                print(f"  #{game['id']} {game['position_label']} {game['paipu_uuid'][:8]} "
                      f"{rounds} 局", flush=True)
        if dry_run:
            print("dry-run：未提交。", flush=True)
            return {"submitted": 0, "errors": errors, "cost": estimated, "rounds": rounds_total}
        if estimated > int(left_np or 0):
            raise NagaError(f"预计需要 {estimated} NP，账户只剩 {left_np} NP，已停止")

        for index, (game, rounds) in enumerate(usable, 1):
            if verbose:
                print(f"\n[{index}/{len(usable)}] #{game['id']} {game['position_label']} "
                      f"({rounds} 局)", flush=True)
            try:
                result = submit_game(
                    game, token=token, player_types=player_types,
                    wait_seconds=wait_seconds, starts_mode=starts_mode,
                    verbose=verbose,
                )
                db.set_naga_url(conn, int(game["id"]), result["proxy_url"])
                conn.commit()
                submitted += 1
            except Exception as exc:  # noqa: BLE001
                errors.append({"id": game["id"], "position": game["position_label"],
                               "error": str(exc)})
                if verbose:
                    print(f"  失败：{exc}", flush=True)
            time.sleep(2)

        final_account = get_account(token)
        final_np = main_account(final_account).get("left_np")
        if verbose:
            print(f"\n批量完成：成功 {submitted}/{len(usable)}，失败 {len(errors)}，"
                  f"剩余 {final_np} NP", flush=True)
            for err in errors:
                print(f"  ERROR #{err['id']} {err['position']}：{err['error']}", flush=True)
        return {"submitted": submitted, "errors": errors,
                "cost": estimated, "rounds": rounds_total, "left_np": final_np}
    finally:
        conn.close()


def link_existing(
    game,
    *,
    token: str,
    player_types: str = DEFAULT_PLAYER_TYPES,
    report_id: str | None = None,
    verbose: bool = True,
) -> dict:
    """批次中断后，从已有报告里找回并回填 naga_url（不重新提交）。"""
    path_value = str(game["processed_json_path"] or "")
    if not path_value:
        raise NagaError("该半庄没有 processed_json_path")
    data = json.loads(config.abs_path(path_value).read_text(encoding="utf-8"))
    names = {str(x) for x in data.get("name") or []}
    account = get_account(token)
    entry = find_existing_report(account, names, player_types, report_id)
    if not entry:
        raise NagaError("没有找到匹配的已有 NAGA 报告")
    raw, _players, rid, ptypes = report_fields(entry)
    proxy, direct = report_urls(rid)
    result = {
        "report_id": rid, "raw_name": raw, "proxy_url": proxy, "direct_url": direct,
        "players": (entry.get("data") or [None, []])[1],
    }
    if verbose:
        print(f"匹配报告：{raw} ({rid})", flush=True)
        print(f"  {result['players']}", flush=True)
        print(f"  {proxy}", flush=True)
    return result


def submit_game(
    game,
    *,
    token: str,
    player_types: str = DEFAULT_PLAYER_TYPES,
    wait_seconds: float = 300,
    poll_interval: float = 10,
    use_raw: bool = False,
    starts_mode: str = "shift75",
    verbose: bool = True,
) -> dict:
    path_value = str(game["local_json_path"] if use_raw else game["processed_json_path"])
    if not path_value:
        raise NagaError(
            "该半庄没有处理后 JSON；先跑 main.py process，或用 --raw 投原始 JSON"
        )
    path = config.abs_path(path_value)
    if not path.exists():
        raise NagaError(f"JSON 不存在：{path}")
    data = normalize_for_naga(
        json.loads(path.read_text(encoding="utf-8")), starts_mode=starts_mode
    )
    if not isinstance(data.get("log"), list) or not data["log"]:
        raise NagaError("JSON 缺少 log")
    rounds = len(data["log"])
    names = {str(x) for x in data.get("name") or []}
    if len(names) != 4:
        raise NagaError("JSON 缺少四家 name")

    token = token.strip()
    account = get_account(token)
    acc = main_account(account)
    account_id = acc.get("id")
    before_raw = {report_fields(e)[0] for _, e in iter_reports(account)}
    left_np = acc.get("left_np")

    compact = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    text = "https://tenhou.net/6/#json=" + compact
    payload = {
        "text": text,
        "player_types": player_types,
        "id": str(account_id),
        "mortal": "0",
        "mortal_seat_id": "0",
        "mortal_model": json.dumps(["4.1a"]),
    }
    if verbose:
        type_names = ",".join(PLAYER_TYPE_NAMES.get(int(x), x) for x in player_types.split(",") if x.strip())
        print(f"投递 {game['position_label']} {game['paipu_uuid'][:8]}："
              f"{rounds} 局，NAGA 类型 {player_types} ({type_names})，"
              f"账号 {account_id}，剩余 {left_np} NP", flush=True)
    resp = _request(BASE + ANALYZE_PATH, token, data=payload, timeout=300)
    if verbose:
        print(f"提交返回：status={resp.get('status')} msg={resp.get('msg')}", flush=True)
    if resp.get("status") != 1:
        msg = str(resp.get("msg") or "")
        # 已解析过：直接找已有报告
        if not ("已被解析过" in msg or "すでに" in msg or "已在处理中" in msg):
            raise NagaError(f"NAGA 提交失败：{msg}")

    deadline = time.monotonic() + max(30.0, wait_seconds)
    entry = None
    while time.monotonic() < deadline:
        account = get_account(token)
        entry = find_report(account, names, before_raw, player_types)
        if entry:
            break
        if verbose:
            print("  等待 NAGA 报告生成 ...", flush=True)
        time.sleep(max(2.0, poll_interval))
    if entry is None:
        raise NagaError(f"等待 NAGA 报告超时（{wait_seconds:.0f}s）")

    raw_name, _players, report_id, _ptypes = report_fields(entry)
    proxy, direct = report_urls(report_id)
    entry_data = entry.get("data") or []
    result = {
        "report_id": report_id,
        "raw_name": raw_name,
        "proxy_url": proxy,
        "direct_url": direct,
        "players": entry_data[1] if len(entry_data) > 1 else [],
        "account_id": account_id,
        "left_np": main_account(account).get("left_np"),
    }
    if verbose:
        print(f"报告：{raw_name}", flush=True)
        print(f"  {result['players']}", flush=True)
        print(f"  {proxy}", flush=True)
    return result
