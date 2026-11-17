"""主流程：抓取大凤林赛程 -> 入库 -> 抓取雀魂牌谱 -> 转天凤 JSON -> 回填四家信息。"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import config
import db
import league_client
import relay_client
import tenhou_meta


def _cache_round(round_no: int, data: dict) -> None:
    path = config.LEAGUE_CACHE_DIR / f"cid{config.CID}_r{round_no}.json"
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _rounds_to_scan(admin: dict, classes: list[dict], requested: list[int] | None) -> list[int]:
    if requested:
        return sorted(set(requested))
    configured = int(admin.get("c_round") or 0)
    from_classes = {int(c.get("round", 0)) for c in classes if int(c.get("round", 0)) > 0}
    upper = max([configured, *from_classes, 1])
    # 已配置但尚未开赛的轮次接口会返回 []；最多扫到 20 轮，避免异常配置。
    return list(range(1, min(upper, 20) + 1))


def update(
    *,
    rounds: list[int] | None = None,
    limit: int | None = None,
    no_fetch: bool = False,
    force: bool = False,
    mode: str = "relay",
    verbose: bool = True,
) -> dict:
    admin = league_client.get_admin()
    teams = league_client.get_teams()
    classes = league_client.get_classes()

    target_tids = league_client.find_team_ids(teams, config.TEAM_NAME)
    if not target_tids:
        raise SystemExit(f"赛事 {config.CID} 中找不到队伍「{config.TEAM_NAME}」")

    positions = league_client.parse_positions(admin)
    start_points = int(admin.get("c_s_po") or 0)
    class_by_rid = {int(c["rid"]): c for c in classes}

    conn = db.connect()
    try:
        db.upsert_competition(conn, config.CID, str(admin.get("c_name", "")),
                              int(admin.get("c_round") or 0), start_points)
        for tid, team in teams.items():
            db.upsert_team(
                conn, config.CID, int(tid), str(team.get("t_name", "")),
                str(team.get("t_player", "")), str(team.get("t_sub", "")),
            )

        rounds_list = _rounds_to_scan(admin, classes, rounds)
        target_rids: list[int] = []
        inserted = 0

        for round_no in rounds_list:
            data = league_client.get_round(config.CID, round_no)
            if not data:
                if verbose:
                    print(f"第 {round_no} 轮：暂无数据")
                continue
            _cache_round(round_no, data)

            round_games = 0
            round_rids: set[int] = set()
            for rid_text, entries in data.items():
                rid = int(rid_text)
                cls = class_by_rid.get(rid)
                if not cls:
                    continue
                side = league_client.match_team_side(cls, target_tids)
                if side is None:
                    continue
                tids = [int(cls.get(f"tid{i}", 0) or 0) for i in range(1, 5)]
                db.upsert_match(
                    conn, config.CID, rid, int(cls.get("round", round_no)),
                    int(cls.get("t_class", 0) or 0), str(cls.get("code", "")), tids,
                )
                target_rids.append(rid)
                round_rids.add(rid)
                if verbose and len(entries) != 10:
                    print(f"    WARN rid={rid} 有 {len(entries)} 个半庄（预期 10 个）",
                          flush=True)

                for idx, entry in enumerate(entries):
                    rowi = int(entry.get("rowi", idx))
                    position, pindex, plabel = league_client.position_label(positions, rowi)
                    url = str(entry.get("url", "")).strip()
                    uuid = league_client.paipu_uuid(url)
                    if not url or not uuid:
                        continue

                    existed = conn.execute(
                        "SELECT id FROM games WHERE cid=? AND rid=? AND rowi=?",
                        (config.CID, rid, rowi),
                    ).fetchone()

                    game = {
                        "cid": config.CID,
                        "rid": rid,
                        "round_no": int(cls.get("round", round_no)),
                        "class_no": int(cls.get("t_class", 0) or 0),
                        "rowi": rowi,
                        "position": position,
                        "position_index": pindex,
                        "position_label": plabel,
                        "game_date": league_client.game_date(entry),
                        "paipu_url": url,
                        "paipu_uuid": uuid,
                        "start_points": start_points,
                    }
                    game_id = db.upsert_game(conn, game)
                    if existed is None:
                        inserted += 1

                    players = _players_from_entry(entry, cls, teams, target_tids, start_points)
                    db.upsert_league_players(conn, game_id, players)
                    round_games += 1

            conn.commit()
            if verbose:
                print(f"第 {round_no} 轮：{round_games} 个半庄已入库"
                      f"（共 {len(data)} 场对阵，目标队伍参加 {len(round_rids)} 场）")

        fetched = 0
        failed = 0
        if not no_fetch:
            fetch_round = rounds[0] if (rounds and len(rounds) == 1) else None
            fetched, failed = fetch_pending(
                conn, limit=limit, force=force, verbose=verbose, mode=mode,
                round_no=fetch_round,
            )

        info = db.stats(conn, config.CID)
        if verbose:
            print()
            print(f"数据库：{config.DB_PATH}")
            print(f"半庄总数 {info['total']['total']}，已抓取 {info['total']['fetched']}，"
                  f"失败 {info['total']['errors'] or 0}，待抓取 {info['total']['pending'] or 0}")
            for row in info["by_round"]:
                print(f"  第 {row['round_no']} 轮：{row['fetched']}/{row['total']}")
            if not no_fetch:
                print(f"本次新抓取 {fetched} 个，失败 {failed} 个")
        return {"inserted": inserted, "fetched": fetched, "failed": failed, "stats": info}
    finally:
        conn.close()


def _players_from_entry(entry: dict, cls: dict, teams: dict, target_tids: set[int],
                        start_points: int) -> list[dict]:
    names = [str(entry.get(f"name{i}", "")) for i in range(1, 5)]
    ends: list[int | None] = []
    deltas: list[int | None] = []
    for i in range(1, 5):
        try:
            ends.append(int(entry.get(f"num{i}")))
        except (TypeError, ValueError):
            ends.append(None)
        try:
            deltas.append(int(entry.get(f"pint{i}")))
        except (TypeError, ValueError):
            deltas.append(None)
    # 名次按终局点数降序
    order = sorted(range(4), key=lambda i: (-(ends[i] if ends[i] is not None else -10**9), i))
    ranks = [0] * 4
    for rank, idx in enumerate(order, 1):
        ranks[idx] = rank

    players = []
    for i in range(4):
        tid = int(cls.get(f"tid{i + 1}", 0) or 0)
        end = ends[i]
        delta = deltas[i]
        if delta is None and end is not None and start_points:
            # 兜底：只有第一局能用 100000 直接算；后续局必须用 pint。
            delta = end - start_points
        start = (end - delta) if (end is not None and delta is not None) else None
        players.append({
            "player_order": i + 1,
            "seat": None,
            "name": names[i],
            "team_tid": tid,
            "team_name": league_client.team_name(teams, tid),
            "start_score": start,
            "end_score": end,
            "delta": delta,
            "rank": ranks[i],
            "is_target_team": tid in target_tids,
        })
    return players


def _fallback_end_by_name(data: dict, players: list) -> list[int] | None:
    by_name = {p["name"]: p["end_score"] for p in players if p["end_score"] is not None}
    names = data.get("name") or []
    if len(names) != 4:
        return None
    values = [by_name.get(str(n)) for n in names]
    if any(v is None for v in values):
        return None
    return [int(v) for v in values]


def fetch_pending(conn, *, limit: int | None = None, force: bool = False,
                  verbose: bool = True, mode: str = "relay",
                  round_no: int | None = None) -> tuple[int, int]:
    if force:
        rows = db.all_games(conn, config.CID)
        if round_no is not None:
            rows = [r for r in rows if int(r["round_no"]) == round_no]
    else:
        rows = db.pending_games(conn, config.CID, round_no=round_no)
        # 标记 fetched 但本地文件被删/丢失的，也要重抓
        seen = {int(r["id"]) for r in rows}
        for r in db.all_games(conn, config.CID):
            if round_no is not None and int(r["round_no"]) != round_no:
                continue
            if int(r["id"]) in seen:
                continue
            path = str(r["local_json_path"] or "")
            if r["fetch_status"] == "fetched" and path and not config.abs_path(path).exists():
                rows.append(r)
        rows.sort(key=lambda r: (int(r["round_no"]), int(r["rowi"])))
    if limit:
        rows = rows[:limit]
    if not rows:
        if verbose:
            print("没有待抓取的半庄。")
        return 0, 0

    fetched = 0
    failed = 0
    total = len(rows)
    for index, row in enumerate(rows, 1):
        game_id = int(row["id"])
        uuid = str(row["paipu_uuid"])
        url = str(row["paipu_url"])
        out_path = config.TENHOU_DIR / f"{uuid}.json"
        players = db.players_of(conn, game_id)
        expected_names = [str(p["name"]) for p in players]

        if verbose:
            print(f"[{index}/{total}] 第{row['round_no']}轮 {row['position_label']} "
                  f"{uuid[:8]}…", flush=True)

        meta = None
        data = None
        if out_path.exists() and out_path.stat().st_size > 0 and not force:
            try:
                data = json.loads(out_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                if verbose:
                    print(f"    本地 JSON 损坏，重新抓取：{exc}")
                data = None
        if data is None:
            try:
                if mode == "local":
                    import majsoul_local

                    data = majsoul_local.fetch_tenhou(uuid, out_path, force=force)
                else:
                    data = relay_client.fetch_tenhou(
                        url, out_path, force=force,
                        log=lambda msg: print(msg, flush=True) if verbose else None,
                    )
            except relay_client.RelayUpstreamLimited as exc:
                failed += 1
                db.mark_fetch_error(conn, game_id, str(exc))
                conn.commit()
                if verbose:
                    print(f"    失败（雀魂拉取限额，停止本轮）：{exc}", flush=True)
                break
            except Exception as exc:  # noqa: BLE001 - 单条失败不应中断整批
                failed += 1
                db.mark_fetch_error(conn, game_id, str(exc))
                conn.commit()
                if verbose:
                    print(f"    失败：{exc}", flush=True)
                if index < total:
                    time.sleep(2.0 if mode == "local" else config.FETCH_DELAY_SECONDS)
                continue

            if index < total:
                time.sleep(2.0 if mode == "local" else config.FETCH_DELAY_SECONDS)

        try:
            fallback = _fallback_end_by_name(data, players)
            meta = tenhou_meta.extract(data, fallback_end=fallback)
            warnings = tenhou_meta.validate(meta, expected_names)
            expected_ref = uuid.split("_a", 1)[0]
            if meta.ref and meta.ref != expected_ref:
                warnings.append(f"牌谱 ref 与 UUID 不一致：{meta.ref} != {expected_ref}")
            warnings += db.apply_tenhou(conn, game_id, meta, config.rel(out_path))
            conn.commit()
            fetched += 1
            if verbose:
                score_text = " / ".join(
                    f"{n} {e}" for n, e in zip(meta.names, meta.end_scores)
                )
                print(f"    完成 {meta.rounds} 局：{score_text}", flush=True)
                for warning in warnings:
                    print(f"    WARN {warning}", flush=True)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            db.mark_fetch_error(conn, game_id, f"解析失败：{exc}")
            conn.commit()
            if verbose:
                print(f"    解析失败：{exc}")

    return fetched, failed


def resolve_game(conn, ident: str):
    ident = str(ident).strip()
    if ident.isdigit():
        row = conn.execute("SELECT * FROM games WHERE id=?", (int(ident),)).fetchone()
        if row:
            return row
    row = conn.execute(
        "SELECT * FROM games WHERE paipu_uuid=? OR paipu_uuid LIKE ? "
        "ORDER BY game_date DESC LIMIT 1",
        (ident, ident + "%"),
    ).fetchone()
    if row:
        return row
    raise SystemExit(f"找不到半庄：{ident}")


def report(round_no: int | None = None, missing_only: bool = False, summary: bool = False) -> None:
    conn = db.connect()
    try:
        sql = "SELECT * FROM games WHERE cid=?"
        params: list[object] = [config.CID]
        if round_no is not None:
            sql += " AND round_no=?"
            params.append(round_no)
        if missing_only:
            sql += " AND fetch_status<>'fetched'"
        sql += " ORDER BY round_no, rowi"
        games = list(conn.execute(sql, params))
        for game in games:
            players = db.players_of(conn, int(game["id"]))
            if summary:
                print(f"#{game['id']} R{game['round_no']} {game['position_label']} "
                      f"{game['paipu_uuid'][:8]} {game['fetch_status']} "
                      f"{game['local_json_path'] or '-'}")
            else:
                print(f"\n#{game['id']} 第{game['round_no']}轮 {game['position_label']} "
                      f"{game['game_date']}  {game['fetch_status']}")
                print(f"  paipu : {game['paipu_url']}")
                print(f"  json  : {game['local_json_path'] or '-'}")
                print(f"  proc  : {game['processed_json_path'] or '-'}")
                print(f"  naga  : {game['naga_url'] or '-'}")
                if game["fetch_error"]:
                    print(f"  error : {game['fetch_error']}")
            if summary:
                continue
            for p in players:
                seat = "-" if p["seat"] is None else p["seat"]
                delta = p["delta"]
                delta_text = f"{delta:+d}" if delta is not None else "-"
                end = p["end_score"] if p["end_score"] is not None else "-"
                start = p["start_score"] if p["start_score"] is not None else "-"
                mark = "*" if p["is_target_team"] else " "
                alias = ""
                if p["league_name"] and p["league_name"] != p["name"]:
                    alias = f" [000:{p['league_name']}]"
                print(f"  {mark}座{seat} {p['name']:<16}{alias} {start:>7} → {end:>7} "
                      f"({delta_text:>7})  {p['rank'] or '-'}位  {p['team_name']}")
    finally:
        conn.close()


def verify(round_no: int | None = None) -> int:
    """检查已抓取牌谱与赛事接口是否一致。返回错误数。"""
    conn = db.connect()
    errors = 0
    checked = 0
    try:
        sql = "SELECT * FROM games WHERE cid=? AND fetch_status='fetched'"
        params: list[object] = [config.CID]
        if round_no is not None:
            sql += " AND round_no=?"
            params.append(round_no)
        games = list(conn.execute(sql, params))
        cache: dict[int, dict] = {}
        for game in games:
            checked += 1
            label = f"#{game['id']} R{game['round_no']} {game['position_label']}"
            path = config.abs_path(str(game["local_json_path"])) if game["local_json_path"] else None
            if not path or not path.exists():
                print(f"{label} ERROR 本地 JSON 不存在：{game['local_json_path']}")
                errors += 1
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                meta = tenhou_meta.extract(data)
            except Exception as exc:  # noqa: BLE001
                print(f"{label} ERROR JSON 解析失败：{exc}")
                errors += 1
                continue

            problems = tenhou_meta.validate(meta)
            if sum(meta.start_scores) != 4 * int(game["start_points"] or 0):
                problems.append(
                    f"开局总和 {sum(meta.start_scores)} != 4 × {game['start_points']}"
                )
            if sum(meta.deltas) != 0:
                problems.append(f"得失总和 {sum(meta.deltas)} != 0")

            rnd = int(game["round_no"])
            if rnd not in cache:
                cache_path = config.LEAGUE_CACHE_DIR / f"cid{config.CID}_r{rnd}.json"
                if cache_path.exists():
                    cache[rnd] = json.loads(cache_path.read_text(encoding="utf-8"))
                else:
                    cache[rnd] = {}
            entry_list = cache[rnd].get(str(game["rid"])) or []
            entry = next(
                (e for e in entry_list if int(e.get("rowi", -1)) == int(game["rowi"])),
                None,
            )
            warning: list[str] = []
            json_nums = sorted(zip(meta.end_scores, meta.deltas))
            if entry:
                league_nums = sorted(
                    (int(entry.get(f"num{i}", 0)), int(entry.get(f"pint{i}", 0)))
                    for i in range(1, 5)
                )
                league_names = sorted(str(entry.get(f"name{i}", "")) for i in range(1, 5))
                if league_nums != json_nums:
                    problems.append(f"点数与 000.mk 不一致：{json_nums} vs {league_nums}")
                if league_names != sorted(meta.names):
                    warning.append(
                        f"姓名与 000.mk 不一致（可能是游戏内改名/别名）：{meta.names} vs {league_names}"
                    )
            else:
                problems.append("赛事缓存里找不到对应 rowi，无法交叉校验")

            db_rows = db.players_of(conn, int(game["id"]))
            db_nums = sorted(
                (int(p["end_score"]), int(p["delta"]))
                for p in db_rows
                if p["end_score"] is not None and p["delta"] is not None
            )
            if len(db_rows) != 4 or db_nums != json_nums:
                problems.append(f"数据库四家点数与 JSON 不一致：{db_nums}")
            db_names = sorted(str(p["name"]) for p in db_rows)
            if db_names != sorted(meta.names):
                warning.append(f"数据库姓名与 JSON 不一致：{db_names} vs {sorted(meta.names)}")

            if problems:
                errors += len(problems)
                print(f"{label} ERROR")
                for problem in problems:
                    print(f"    - {problem}")
            else:
                print(f"{label} OK  {meta.rounds} 局  {sum(meta.end_scores)} 点")
            for item in warning:
                print(f"    WARN {item}")
    finally:
        conn.close()
    print(f"\n检查 {checked} 个半庄，{errors} 个问题")
    return errors


def export_csv(path: Path) -> int:
    """把 games + game_players 展平成 CSV（Excel 可直接打开）。"""
    import csv

    conn = db.connect()
    try:
        rows = list(conn.execute(
            """SELECT g.id AS game_id, g.round_no, g.position_label, g.game_date,
                      g.paipu_url, g.local_json_path, g.processed_json_path,
                      g.naga_url, g.fetch_status,
                      p.seat, p.name, p.league_name, p.team_name,
                      p.start_score, p.end_score, p.delta, p.rank, p.is_target_team
               FROM games g
               LEFT JOIN game_players p ON p.game_id = g.id
               WHERE g.cid=?
               ORDER BY g.round_no, g.rowi, p.seat IS NULL, p.seat, p.player_order""",
            (config.CID,),
        ))
    finally:
        conn.close()

    path.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "game_id", "round_no", "position_label", "game_date",
        "paipu_url", "local_json_path", "processed_json_path", "naga_url",
        "fetch_status", "seat", "name", "league_name", "team_name",
        "start_score", "end_score", "delta", "rank", "is_target_team",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(columns)
        for row in rows:
            writer.writerow([row[c] if row[c] is not None else "" for c in columns])
    print(f"已导出 {len(rows)} 行：{path}", flush=True)
    return len(rows)


def status() -> None:
    conn = db.connect()
    try:
        info = db.stats(conn, config.CID)
        total = info["total"]
        print(f"数据库：{config.DB_PATH}")
        print(f"半庄 {total['total']}，已抓取 {total['fetched']}，失败 {total['errors'] or 0}，"
              f"待抓取 {total['pending'] or 0}，已处理 {total['processed'] or 0}，"
              f"已有 NAGA 链接 {total['naga'] or 0}")
        for row in info["by_round"]:
            print(f"  第 {row['round_no']} 轮：{row['fetched']}/{row['total']}")
        errors = list(conn.execute(
            "SELECT id, round_no, position_label, fetch_error FROM games "
            "WHERE cid=? AND fetch_status='error' ORDER BY round_no, rowi",
            (config.CID,),
        ))
        for row in errors:
            print(f"  ERROR #{row['id']} 第{row['round_no']}轮 {row['position_label']}：{row['fetch_error']}")
    finally:
        conn.close()
