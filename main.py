"""比赛牌谱处理 CLI。

常用：
  python main.py update                 # 抓赛程 + 抓新牌谱 + 入库
  python main.py update --round 3       # 只处理第 3 轮
  python main.py update --no-fetch      # 只刷新赛事元数据
  python main.py fetch --limit 5        # 只抓待抓取的牌谱
  python main.py status                 # 数据库概览
  python main.py report                 # 明细
  python main.py set-naga --id 12 --url <NAGA链接>
  python main.py set-processed --id 12 --path data/processed/xxx.json
"""

from __future__ import annotations

import argparse
import json
import sys

import config
import db
import pipeline
import relay_client


def _utf8_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def cmd_update(args) -> int:
    pipeline.update(
        rounds=args.round or None,
        limit=args.limit,
        no_fetch=args.no_fetch,
        force=args.force,
        mode=args.mode,
        verbose=not args.quiet,
    )
    return 0


def cmd_fetch(args) -> int:
    conn = db.connect()
    try:
        fetched, failed = pipeline.fetch_pending(
            conn, limit=args.limit, force=args.force,
            verbose=not args.quiet, mode=args.mode, round_no=args.round,
        )
        print(f"抓取完成：成功 {fetched}，失败 {failed}")
        return 0 if failed == 0 else 1
    finally:
        conn.close()


def cmd_status(_args) -> int:
    pipeline.status()
    return 0


def cmd_process(args) -> int:
    import process_pairs

    stats = process_pairs.process_all(
        round_no=args.round, force=args.force, dry_run=args.dry_run
    )
    return 0 if stats["errors"] == 0 else 1


def cmd_verify_processed(args) -> int:
    import process_pairs

    errors = process_pairs.verify_processed(round_no=args.round)
    return 0 if errors == 0 else 1


def cmd_export(args) -> int:
    from pathlib import Path

    out = Path(args.out) if args.out else config.DATA_DIR / "games.csv"
    pipeline.export_csv(out)
    return 0


def cmd_verify(args) -> int:
    errors = pipeline.verify(round_no=args.round)
    return 0 if errors == 0 else 1


def cmd_report(args) -> int:
    pipeline.report(
        round_no=args.round,
        missing_only=args.missing,
        summary=args.summary,
    )
    return 0


def _resolve(args):
    conn = db.connect()
    try:
        ident = str(args.id or args.uuid or "").strip()
        if not ident:
            raise SystemExit("请提供 --id 或 --uuid")
        return conn, pipeline.resolve_game(conn, ident)
    except Exception:
        conn.close()
        raise


def cmd_naga(args) -> int:
    import naga_client

    conn, game = _resolve(args)
    try:
        if game["naga_url"] and not args.force:
            print(f"#{game['id']} {game['position_label']} 已有 NAGA 链接：")
            print(game["naga_url"])
            print("如需重新投递，加 --force")
            return 0
        token = naga_client.load_token(args.token_file)
        result = naga_client.submit_game(
            game,
            token=token,
            player_types=args.player_types,
            wait_seconds=args.wait,
            use_raw=args.raw,
            starts_mode=args.starts,
        )
        db.set_naga_url(conn, int(game["id"]), result["proxy_url"])
        conn.commit()
        print(f"#{game['id']} {game['position_label']} NAGA 链接已写入 naga_url")
        print(result["proxy_url"])
        return 0
    finally:
        conn.close()


def cmd_naga_link(args) -> int:
    import naga_client

    conn, game = _resolve(args)
    try:
        token = naga_client.load_token(args.token_file)
        result = naga_client.link_existing(
            game, token=token, player_types=args.player_types,
            report_id=args.report_id,
        )
        db.set_naga_url(conn, int(game["id"]), result["proxy_url"])
        conn.commit()
        print(f"#{game['id']} {game['position_label']} NAGA 链接已写入")
        print(result["proxy_url"])
        return 0
    finally:
        conn.close()


def cmd_naga_batch(args) -> int:
    import naga_client

    token = naga_client.load_token(args.token_file)
    stats = naga_client.batch_round(
        args.round,
        token=token,
        player_types=args.player_types,
        wait_seconds=args.wait,
        force=args.force,
        limit=args.limit,
        dry_run=args.dry_run,
        starts_mode=args.starts,
    )
    return 0 if not stats["errors"] else 1


def cmd_set_naga(args) -> int:
    conn, game = _resolve(args)
    try:
        url = "" if args.clear else str(args.url or "").strip()
        if not url and not args.clear:
            raise SystemExit("请提供 --url，或用 --clear 清空")
        db.set_naga_url(conn, int(game["id"]), url)
        conn.commit()
        print(f"#{game['id']} {game['position_label']} NAGA 链接已{'清空' if args.clear else '更新'}")
        if url:
            print(url)
        return 0
    finally:
        conn.close()


def cmd_set_processed(args) -> int:
    conn, game = _resolve(args)
    try:
        path = "" if args.clear else str(args.path or "").strip()
        if not path and not args.clear:
            raise SystemExit("请提供 --path，或用 --clear 清空")
        if path:
            from pathlib import Path

            candidate = Path(path)
            if candidate.is_absolute():
                path = config.rel(candidate)
        db.set_processed_json(conn, int(game["id"]), path)
        conn.commit()
        print(f"#{game['id']} {game['position_label']} 处理后 JSON 已{'清空' if args.clear else '更新'}")
        if path:
            print(path)
        return 0
    finally:
        conn.close()


def cmd_service_info(_args) -> int:
    info = relay_client.service_info()
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


def cmd_convert_raw(args) -> int:
    from pathlib import Path

    import majsoul_local

    out = Path(args.output) if args.output else config.TENHOU_DIR / (Path(args.input).stem + ".json")
    result = majsoul_local.convert_raw(Path(args.input), out)
    print(f"已转换：{out}（{len(result.get('log', []))} 局）")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="大凤林联赛（000.mk）雀魂牌谱抓取 / 转天凤 JSON / 本地数据库"
    )
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("update", help="抓取赛程并抓取新牌谱（默认命令）")
    p.add_argument("--round", type=int, action="append", help="只处理指定轮次，可重复")
    p.add_argument("--limit", type=int, help="本次最多抓取多少个半庄")
    p.add_argument("--no-fetch", action="store_true", help="只刷新赛事元数据，不抓牌谱")
    p.add_argument("--force", action="store_true", help="忽略已抓取状态，重新下载并覆盖")
    p.add_argument("--mode", choices=("relay", "local"), default="relay",
                   help="relay=公开中转（默认，有限流）；local=本地雀魂账号")
    p.add_argument("-q", "--quiet", action="store_true", help="少输出")
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("fetch", help="只抓取数据库中待抓取的牌谱")
    p.add_argument("--round", type=int, help="只抓取指定轮次")
    p.add_argument("--limit", type=int)
    p.add_argument("--force", action="store_true")
    p.add_argument("--mode", choices=("relay", "local"), default="relay")
    p.add_argument("-q", "--quiet", action="store_true")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("status", help="数据库概览")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("export", help="导出展平 CSV（Excel 可打开）")
    p.add_argument("--out", help="输出路径，默认 data/games.csv")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("process", help="按本地 JSON 规则转换 X1/X2 开局点数")
    p.add_argument("--round", type=int, help="只处理指定轮次")
    p.add_argument("--force", action="store_true", help="覆盖已存在的处理后 JSON")
    p.add_argument("--dry-run", action="store_true", help="只打印计划，不写文件/数据库")
    p.set_defaults(func=cmd_process)

    p = sub.add_parser("verify-processed", help="校验处理后 JSON 是否符合规则")
    p.add_argument("--round", type=int)
    p.set_defaults(func=cmd_verify_processed)

    p = sub.add_parser("verify", help="校验已抓取 JSON 与赛事接口/数据库是否一致")
    p.add_argument("--round", type=int)
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("report", help="打印半庄与四家明细")
    p.add_argument("--round", type=int)
    p.add_argument("--missing", action="store_true", help="只看未抓取的")
    p.add_argument("--summary", action="store_true", help="每个半庄只打一行")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("naga-link", help="从已有 NAGA 报告找回并回填 naga_url（不重新提交）")
    p.add_argument("--id", help="数据库 games.id")
    p.add_argument("--uuid", help="牌谱 UUID（可只给前缀）")
    p.add_argument("--token-file", help="ricochet token 文件")
    p.add_argument("--player-types", default="2,4,6",
                   help="NAGA 类型，默认 2,4,6（ニシキ+カガシ+ウロボロス）")
    p.add_argument("--report-id", help="指定 report_id（X1/X2 同名时必填）")
    p.set_defaults(func=cmd_naga_link)

    p = sub.add_parser("naga-batch", help="批量投递某一轮所有半庄到 NAGA")
    p.add_argument("--round", type=int, required=True, help="轮次")
    p.add_argument("--token-file", help="ricochet token 文件")
    p.add_argument("--player-types", default="2,4,6",
                   help="NAGA 类型，默认 2,4,6（ニシキ+カガシ+ウロボロス）")
    p.add_argument("--wait", type=float, default=900, help="每场等待报告秒数，默认 900")
    p.add_argument("--limit", type=int)
    p.add_argument("--starts", choices=("shift75", "shift25", "all", "shift", "first", "none"), default="shift75",
                   help="开局兼容模式：shift75=每人减75000，首局和100000且保留X1得失（默认，已验证）；shift25=平移到标准25000+实际得失滚动（可用，丢X1得失）；all=全部局100000；shift=平移100000（405）；first=仅首局（405）")
    p.add_argument("--force", action="store_true", help="已有 naga_url 也重新投递")
    p.add_argument("--dry-run", action="store_true", help="只估算 NP 和列出计划")
    p.set_defaults(func=cmd_naga_batch)

    p = sub.add_parser("naga", help="通过 ricochet.cn 投递处理后 JSON，等待并回填 NAGA 链接")
    p.add_argument("--id", help="数据库 games.id")
    p.add_argument("--uuid", help="牌谱 UUID（可只给前缀）")
    p.add_argument("--token-file", help="ricochet token 文件；默认 data/ricochet_token.txt 或 RICOCHET_TOKEN")
    p.add_argument("--player-types", default="2,4",
                   help="NAGA 类型，默认 2,4（2=ニシキ, 4=カガシ）")
    p.add_argument("--wait", type=float, default=300, help="等待报告生成的秒数，默认 300")
    p.add_argument("--raw", action="store_true", help="投原始 local_json_path 而不是 processed_json_path")
    p.add_argument("--starts", choices=("shift75", "shift25", "all", "shift", "first", "none"), default="shift75",
                   help="开局兼容模式：shift75=每人减75000，首局和100000且保留X1得失（默认，已验证）；shift25=平移到标准25000+实际得失滚动（可用，丢X1得失）；all=全部局100000；shift=平移100000（405）；first=仅首局（405）")
    p.add_argument("--force", action="store_true", help="已有 naga_url 也重新投递")
    p.set_defaults(func=cmd_naga)

    for name, func, help_text in (
        ("set-naga", cmd_set_naga, "手工填写 NAGA 解析链接"),
        ("set-processed", cmd_set_processed, "填写处理后 JSON 路径"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--id", help="数据库 games.id")
        p.add_argument("--uuid", help="牌谱 UUID（可只给前缀）")
        if name == "set-naga":
            p.add_argument("--url")
        else:
            p.add_argument("--path")
        p.add_argument("--clear", action="store_true")
        p.set_defaults(func=func)

    p = sub.add_parser("service-info", help="查看中转服务状态")
    p.set_defaults(func=cmd_service_info)

    p = sub.add_parser("convert-raw", help="把 ResGameRecord protobuf 文件转成天凤 JSON")
    p.add_argument("--input", required=True, help="原始 protobuf 文件（.pb）")
    p.add_argument("--output", help="输出 JSON 路径")
    p.set_defaults(func=cmd_convert_raw)

    return parser


def main(argv: list[str] | None = None) -> int:
    _utf8_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        args = parser.parse_args(["update"])
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
