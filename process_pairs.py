"""按本地 JSON 规则（不入库）对同一位置的两个半庄（X1/X2）做点数修正。

规则摘要：
- X1 = 先锋1 / 次锋1 / 中坚1 / 副将1：
  该半庄的**每一小局**开局四家都设为 100000。
- X2 = 次锋2 / 中坚2 / 副将2：
  首局开局 = 100000 + 对应 X1 的得失；之后各局按原牌谱的正常滚动
  （实现为给整份 X2 牌谱的每局开局点加同一个偏移量）。
- 先锋2：不调整（原始数据已经是 100000 + 先锋1 得失）。
- 大将1、大将2：不调整。

原始文件保持不变，处理结果写到 data/processed/<uuid>.json，
并把 games.processed_json_path 指向新文件。
"""

from __future__ import annotations

import copy
import csv
import json
import os
from collections import defaultdict
from pathlib import Path

import config
import db

RESET_ROWS = {0, 2, 4, 6}       # 先锋1、次锋1、中坚1、副将1
OFFSET_ROWS = {3, 5, 7}         # 次锋2、中坚2、副将2
NO_ADJUST_ROWS = {1, 8, 9}      # 先锋2、大将1、大将2

BASE = 100000


def _load(path: str | Path) -> dict:
    with Path(path).open("r", encoding="utf-8") as f:
        return json.load(f)


def _x1_info(data: dict, players_db: dict[int, dict]) -> list[dict]:
    """返回 X1 四家：座位、游戏内名、开局、终局、得失。"""
    starts = ([int(x) for x in data["log"][0][1]] + [BASE] * 4)[:4]
    names = ([str(x) for x in data.get("name", [])] + [""] * 4)[:4]
    sc = data.get("sc")
    info = []
    for seat in range(4):
        start = starts[seat]
        if isinstance(sc, list) and len(sc) >= 8:
            final = int(sc[2 * seat])
        else:
            p = players_db.get(seat)
            final = int(p["end_score"]) if p and p.get("end_score") is not None else start
        info.append({
            "seat": seat,
            "name": names[seat],
            "start": start,
            "final": final,
            "net": final - start,
        })
    return info


def map_x2_to_x1(x1_info: list[dict], x2_names: list[str],
                 x2_starts: list[int]) -> tuple[dict[int, dict | None], dict[int, str]]:
    """把 X2 的每个座位映射到 X1 的某个座位。

    优先按游戏内姓名匹配；替补选手（X1 没出现）按“X2 原首局点数 == X1 终局点数”
    匹配，因为 X2 的原首局点数就是 X1 的终局点数按座位轮转后的结果。
    """
    mapping: dict[int, dict | None] = {}
    methods: dict[int, str] = {}
    used: set[int] = set()

    for seat, name in enumerate(x2_names):
        for xi in x1_info:
            if xi["seat"] not in used and xi["name"] == name:
                mapping[seat] = xi
                methods[seat] = "name"
                used.add(xi["seat"])
                break

    for seat, _name in enumerate(x2_names):
        if seat in mapping:
            continue
        candidates = [
            xi for xi in x1_info
            if xi["seat"] not in used and xi["final"] == x2_starts[seat]
        ]
        if candidates:
            # 同座优先；否则取最小座位，保证确定性
            candidates.sort(key=lambda x: (x["seat"] != seat, x["seat"]))
            mapping[seat] = candidates[0]
            methods[seat] = "score"
            used.add(candidates[0]["seat"])

    for seat in range(4):
        if seat in mapping:
            continue
        candidates = [xi for xi in x1_info if xi["seat"] not in used and xi["seat"] == seat]
        if candidates:
            mapping[seat] = candidates[0]
            methods[seat] = "seat"
            used.add(candidates[0]["seat"])
        else:
            mapping[seat] = None
            methods[seat] = "none"

    return mapping, methods


def reset_x1(data: dict) -> dict:
    """X1：每一小局开局四家都设为 100000。"""
    out = copy.deepcopy(data)
    for kyoku in out.get("log", []):
        if len(kyoku) >= 2:
            kyoku[1] = [BASE, BASE, BASE, BASE]
    return out


def offset_x2(data: dict, mapping: dict[int, dict | None]) -> tuple[dict, dict[int, int], dict[int, int]]:
    """X2：首局 = 100000 + X1 得失，整份牌谱加同一个偏移。"""
    out = copy.deepcopy(data)
    original_start = [int(x) for x in out["log"][0][1]]
    new_start: dict[int, int] = {}
    offset: dict[int, int] = {}
    for seat in range(4):
        xi = mapping.get(seat)
        new_start[seat] = BASE + (int(xi["net"]) if xi else 0)
        offset[seat] = new_start[seat] - original_start[seat]
    for kyoku in out.get("log", []):
        if len(kyoku) >= 2:
            kyoku[1] = [int(v) + offset[i] for i, v in enumerate(kyoku[1])]
    return out, new_start, offset


def copy_unchanged(data: dict) -> dict:
    return copy.deepcopy(data)


def _write_json(data: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _players_by_seat(conn, game_id: int) -> dict[int, dict]:
    rows = db.players_of(conn, game_id)
    return {int(r["seat"]): dict(r) for r in rows if r["seat"] is not None}


def _processed_path(uuid: str) -> Path:
    return config.PROCESSED_DIR / f"{uuid}.json"


def _ready(game) -> bool:
    path = str(game["local_json_path"] or "")
    return bool(path) and config.abs_path(path).exists()


def process_all(*, round_no: int | None = None, force: bool = False,
                dry_run: bool = False, verbose: bool = True) -> dict:
    conn = db.connect()
    report_rows: list[dict] = []
    stats = {"processed": 0, "skipped": 0, "errors": 0}
    try:
        sql = "SELECT * FROM games WHERE cid=?"
        params: list[object] = [config.CID]
        if round_no is not None:
            sql += " AND round_no=?"
            params.append(round_no)
        sql += " ORDER BY round_no, rid, rowi"
        groups: dict[tuple[int, int], list] = defaultdict(list)
        for game in conn.execute(sql, params):
            groups[(int(game["round_no"]), int(game["rid"]))].append(game)

        for (rnd, rid), games in sorted(groups.items()):
            by_rowi = {int(g["rowi"]): g for g in games}

            for even in (0, 2, 4, 6, 8):
                x1 = by_rowi.get(even)
                x2 = by_rowi.get(even + 1)

                if even == 8:  # 大将1/2 不调整
                    for game in (x1, x2):
                        if game and _ready(game):
                            _handle_copy(conn, game, report_rows, stats, force or dry_run, dry_run, verbose)
                        elif game and verbose:
                            print(f"  跳过 {game['position_label']} {game['paipu_uuid'][:8]}"
                                  "（尚未抓取）", flush=True)
                    continue

                if even in RESET_ROWS and x1:
                    if _ready(x1):
                        _handle_x1(conn, x1, report_rows, stats, force or dry_run, dry_run, verbose)
                    elif verbose:
                        print(f"  跳过 {x1['position_label']} {x1['paipu_uuid'][:8]}（尚未抓取）",
                              flush=True)

                if x2 is None:
                    if even in RESET_ROWS and x1 and verbose:
                        print(f"  第{rnd}轮 rid={rid} {x1['position_label']} 缺少配对的"
                              f"{x1['position_label'][:-1]}2，已跳过 X2 修正", flush=True)
                    continue

                if not _ready(x2):
                    if verbose:
                        print(f"  跳过 {x2['position_label']} {x2['paipu_uuid'][:8]}（尚未抓取）",
                              flush=True)
                    continue

                if even == 0:  # 先锋2 不调整
                    _handle_copy(conn, x2, report_rows, stats, force or dry_run, dry_run, verbose)
                    continue

                if even in (2, 4, 6):
                    if x1 is None or not _ready(x1):
                        stats["errors"] += 1
                        if verbose:
                            print(f"  ERROR 第{rnd}轮 rid={rid} {x2['position_label']} 找不到可用的 X1，跳过",
                                  flush=True)
                        continue
                    _handle_x2(conn, x1, x2, report_rows, stats, force or dry_run, dry_run, verbose)

        if not dry_run and report_rows:
            _write_report(report_rows)
    finally:
        conn.close()

    if verbose:
        action = "计划处理" if dry_run else "处理完成"
        print(f"\n{action}：{stats['processed']}，跳过 {stats['skipped']}，错误 {stats['errors']}")
    return stats


def _already_done(game, force: bool) -> Path | None:
    path = str(game["processed_json_path"] or "")
    if not force and path:
        p = config.abs_path(path)
        if p.exists():
            return p
    return None


def _handle_x1(conn, game, report_rows, stats, force, dry_run, verbose) -> None:
    existing = _already_done(game, force)
    if existing:
        stats["skipped"] += 1
        if verbose:
            print(f"  跳过 {game['position_label']} {game['paipu_uuid'][:8]}（已处理）", flush=True)
        return
    try:
        original = _load(game["local_json_path"])
        processed = reset_x1(original)
        out_path = _processed_path(str(game["paipu_uuid"]))
        if not dry_run:
            _write_json(processed, out_path)
            db.set_processed_json(conn, int(game["id"]), config.rel(out_path))
            conn.commit()
        stats["processed"] += 1
        report_rows.append({
            "round_no": game["round_no"], "rid": game["rid"],
            "position_x1": game["position_label"], "position_x2": "",
            "seat_x1": "", "name_x1": "", "x1_net": "",
            "seat_x2": "", "name_x2": "", "x2_old_start": "", "x2_new_start": "",
            "rule": "X1_reset_100000",
        })
        if verbose:
            rounds = len(processed.get("log", []))
            print(f"  {game['position_label']} {game['paipu_uuid'][:8]}：{rounds} 局全部重置为 100000",
                  flush=True)
    except Exception as exc:  # noqa: BLE001
        stats["errors"] += 1
        if verbose:
            print(f"  ERROR {game['position_label']} {game['paipu_uuid'][:8]}：{exc}", flush=True)


def _handle_x2(conn, x1_game, x2_game, report_rows, stats, force, dry_run, verbose) -> None:
    existing = _already_done(x2_game, force)
    if existing:
        stats["skipped"] += 1
        if verbose:
            print(f"  跳过 {x2_game['position_label']} {x2_game['paipu_uuid'][:8]}（已处理）", flush=True)
        return
    try:
        x1_data = _load(x1_game["local_json_path"])
        x2_data = _load(x2_game["local_json_path"])
        x1_info = _x1_info(x1_data, _players_by_seat(conn, int(x1_game["id"])))
        x2_names = ([str(x) for x in x2_data.get("name", [])] + [""] * 4)[:4]
        x2_starts = ([int(x) for x in x2_data["log"][0][1]] + [BASE] * 4)[:4]
        mapping, methods = map_x2_to_x1(x1_info, x2_names, x2_starts)
        processed, new_start, offset = offset_x2(x2_data, mapping)

        out_path = _processed_path(str(x2_game["paipu_uuid"]))
        if not dry_run:
            _write_json(processed, out_path)
            db.set_processed_json(conn, int(x2_game["id"]), config.rel(out_path))
            conn.commit()
        stats["processed"] += 1

        for seat in range(4):
            xi = mapping.get(seat)
            report_rows.append({
                "round_no": x2_game["round_no"], "rid": x2_game["rid"],
                "position_x1": x1_game["position_label"], "position_x2": x2_game["position_label"],
                "seat_x1": xi["seat"] if xi else "",
                "name_x1": xi["name"] if xi else "",
                "x1_net": xi["net"] if xi else "",
                "seat_x2": seat, "name_x2": x2_names[seat],
                "x2_old_start": x2_starts[seat], "x2_new_start": new_start[seat],
                "rule": f"X2_offset:{methods.get(seat, 'none')}",
            })
        if verbose:
            print(f"  {x1_game['position_label']} → {x2_game['position_label']} "
                  f"{x2_game['paipu_uuid'][:8]}：首局 "
                  f"{x2_starts} → {[new_start[i] for i in range(4)]}", flush=True)
            for seat in range(4):
                if methods.get(seat) not in ("name", "score"):
                    print(f"    WARN seat{seat} {x2_names[seat]} 映射方式={methods.get(seat)}",
                          flush=True)
    except Exception as exc:  # noqa: BLE001
        stats["errors"] += 1
        if verbose:
            print(f"  ERROR {x2_game['position_label']} {x2_game['paipu_uuid'][:8]}：{exc}",
                  flush=True)


def _handle_copy(conn, game, report_rows, stats, force, dry_run, verbose) -> None:
    existing = _already_done(game, force)
    if existing:
        stats["skipped"] += 1
        if verbose:
            print(f"  跳过 {game['position_label']} {game['paipu_uuid'][:8]}（已处理）", flush=True)
        return
    try:
        data = _load(game["local_json_path"])
        processed = copy_unchanged(data)
        out_path = _processed_path(str(game["paipu_uuid"]))
        if not dry_run:
            _write_json(processed, out_path)
            db.set_processed_json(conn, int(game["id"]), config.rel(out_path))
            conn.commit()
        stats["processed"] += 1
        report_rows.append({
            "round_no": game["round_no"], "rid": game["rid"],
            "position_x1": game["position_label"], "position_x2": "",
            "seat_x1": "", "name_x1": "", "x1_net": "",
            "seat_x2": "", "name_x2": "", "x2_old_start": "", "x2_new_start": "",
            "rule": "unchanged",
        })
        if verbose:
            print(f"  {game['position_label']} {game['paipu_uuid'][:8]}：不调整（原样复制）",
                  flush=True)
    except Exception as exc:  # noqa: BLE001
        stats["errors"] += 1
        if verbose:
            print(f"  ERROR {game['position_label']} {game['paipu_uuid'][:8]}：{exc}", flush=True)


def verify_processed(*, round_no: int | None = None, verbose: bool = True) -> int:
    """校验处理后 JSON 是否严格符合本地 JSON 规则。返回错误数。"""
    conn = db.connect()
    errors = 0
    checked = 0
    try:
        sql = "SELECT * FROM games WHERE cid=? AND fetch_status='fetched'"
        params: list[object] = [config.CID]
        if round_no is not None:
            sql += " AND round_no=?"
            params.append(round_no)
        games = list(conn.execute(sql + " ORDER BY round_no, rid, rowi", params))
        by_key = {(int(g["round_no"]), int(g["rid"]), int(g["rowi"])): g for g in games}

        for game in games:
            checked += 1
            label = f"#{game['id']} R{game['round_no']} {game['position_label']}"
            path = str(game["processed_json_path"] or "")
            if not path:
                print(f"{label} ERROR 未填写 processed_json_path")
                errors += 1
                continue
            p = config.abs_path(path)
            if not p.exists():
                print(f"{label} ERROR 处理后 JSON 不存在：{path}")
                errors += 1
                continue
            try:
                original = _load(game["local_json_path"])
                processed = _load(p)
            except Exception as exc:  # noqa: BLE001
                print(f"{label} ERROR 读取 JSON 失败：{exc}")
                errors += 1
                continue

            problems: list[str] = []
            if original.get("name") != processed.get("name"):
                problems.append("name 被改动")
            if original.get("sc") != processed.get("sc"):
                problems.append("sc 被改动（本项目约定保留原始 sc）")
            if len(original.get("log", [])) != len(processed.get("log", [])):
                problems.append("log 局数被改动")
            for i, (ro, rp) in enumerate(zip(original.get("log", []), processed.get("log", []))):
                if len(ro) != len(rp):
                    problems.append(f"第{i + 1}局元素数被改动")
                    continue
                if ro[0] != rp[0] or ro[-1] != rp[-1]:
                    problems.append(f"第{i + 1}局 seed/结果被改动")
                if [ro[j] for j in range(len(ro)) if j != 1] != [rp[j] for j in range(len(rp)) if j != 1]:
                    problems.append(f"第{i + 1}局除开局点外被改动")

            rowi = int(game["rowi"])
            if rowi in RESET_ROWS:
                for i, rp in enumerate(processed.get("log", [])):
                    if [int(x) for x in rp[1]] != [BASE] * 4:
                        problems.append(f"第{i + 1}局未重置为 100000")
                        break
                # 注意：X1 只重置四家点数，供托（seed[2]）按 MD 保持原样；
                # 因此这一类的“点数总和 + 供托”可能大于 400000，不做该检查。
            elif rowi in NO_ADJUST_ROWS:
                for i, (ro, rp) in enumerate(zip(original.get("log", []), processed.get("log", []))):
                    if [int(x) for x in ro[1]] != [int(x) for x in rp[1]]:
                        problems.append(f"第{i + 1}局不应调整但被改动")
                        break
                    pot = int(rp[0][2])
                    if sum(int(x) for x in rp[1]) + 1000 * pot != 4 * BASE:
                        problems.append(f"第{i + 1}局点数+供托 != 400000")
            elif rowi in OFFSET_ROWS:
                x1_game = by_key.get((int(game["round_no"]), int(game["rid"]), rowi - 1))
                if x1_game is None:
                    problems.append("找不到配对的 X1")
                else:
                    try:
                        x1_data = _load(x1_game["local_json_path"])
                        x1_info = _x1_info(x1_data, _players_by_seat(conn, int(x1_game["id"])))
                        x2_names = ([str(x) for x in processed.get("name", [])] + [""] * 4)[:4]
                        x2_starts = ([int(x) for x in original["log"][0][1]] + [BASE] * 4)[:4]
                        mapping, _methods = map_x2_to_x1(x1_info, x2_names, x2_starts)
                        expected = [BASE + (int(mapping[s]["net"]) if mapping.get(s) else 0)
                                    for s in range(4)]
                        actual = [int(x) for x in processed["log"][0][1]]
                        if actual != expected:
                            problems.append(f"X2 首局 {actual} != 期望 {expected}")
                        # 所有局的偏移必须一致
                        first_offset = [actual[s] - x2_starts[s] for s in range(4)]
                        for i, (ro, rp) in enumerate(zip(original["log"], processed["log"])):
                            off = [int(rp[1][s]) - int(ro[1][s]) for s in range(4)]
                            if off != first_offset:
                                problems.append(f"第{i + 1}局偏移不一致：{off} vs {first_offset}")
                                break
                            pot = int(rp[0][2])
                            if sum(int(x) for x in rp[1]) + 1000 * pot != 4 * BASE:
                                problems.append(f"第{i + 1}局点数+供托 != 400000")
                                break
                    except Exception as exc:  # noqa: BLE001
                        problems.append(f"X2 校验失败：{exc}")

            if problems:
                errors += len(problems)
                print(f"{label} ERROR")
                for item in problems:
                    print(f"    - {item}")
            elif verbose:
                print(f"{label} OK  {len(processed.get('log', []))} 局")
    finally:
        conn.close()
    print(f"\n处理后检查 {checked} 个半庄，{errors} 个问题")
    return errors


def _write_report(rows: list[dict]) -> None:
    path = config.PROCESSED_DIR / "processing_report.csv"
    columns = [
        "round_no", "rid", "position_x1", "position_x2",
        "seat_x1", "name_x1", "x1_net",
        "seat_x2", "name_x2", "x2_old_start", "x2_new_start", "rule",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    print(f"处理报告：{path}", flush=True)
