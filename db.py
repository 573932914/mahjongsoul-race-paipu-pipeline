"""SQLite 本地数据库：赛事、队伍、对阵、半庄、四家明细。

一个“半庄” = 天凤 JSON 的一份牌谱 = 大凤林 c_data 里的一个 rowi 记录。
每轮比赛固定 10 个半庄（先锋1/2、次锋1/2、中坚1/2、副将1/2、大将1/2）。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import config
from tenhou_meta import TenhouMeta

SCHEMA = """
CREATE TABLE IF NOT EXISTS competitions (
    cid           INTEGER PRIMARY KEY,
    name          TEXT NOT NULL DEFAULT '',
    rounds        INTEGER NOT NULL DEFAULT 0,
    start_points  INTEGER NOT NULL DEFAULT 0,
    updated_at    TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS teams (
    cid        INTEGER NOT NULL,
    tid        INTEGER NOT NULL,
    name       TEXT NOT NULL DEFAULT '',
    players    TEXT NOT NULL DEFAULT '',
    substitutes TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (cid, tid)
);

CREATE TABLE IF NOT EXISTS matches (
    cid       INTEGER NOT NULL,
    rid       INTEGER NOT NULL,
    round_no  INTEGER NOT NULL,
    class_no  INTEGER NOT NULL DEFAULT 0,
    code      TEXT NOT NULL DEFAULT '',
    team1     INTEGER NOT NULL DEFAULT 0,
    team2     INTEGER NOT NULL DEFAULT 0,
    team3     INTEGER NOT NULL DEFAULT 0,
    team4     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (cid, rid)
);

CREATE TABLE IF NOT EXISTS games (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    cid                 INTEGER NOT NULL,
    rid                 INTEGER NOT NULL,
    round_no            INTEGER NOT NULL,
    class_no            INTEGER NOT NULL DEFAULT 0,
    rowi                INTEGER NOT NULL,
    position            TEXT NOT NULL DEFAULT '',
    position_index      INTEGER NOT NULL DEFAULT 0,
    position_label      TEXT NOT NULL DEFAULT '',
    game_date           TEXT NOT NULL DEFAULT '',
    paipu_url           TEXT NOT NULL UNIQUE,
    paipu_uuid          TEXT NOT NULL UNIQUE,
    start_points        INTEGER NOT NULL DEFAULT 0,
    rule                TEXT NOT NULL DEFAULT '',
    local_json_path     TEXT NOT NULL DEFAULT '',
    processed_json_path TEXT NOT NULL DEFAULT '',
    naga_url            TEXT NOT NULL DEFAULT '',
    fetch_status        TEXT NOT NULL DEFAULT 'pending',
    fetch_error         TEXT NOT NULL DEFAULT '',
    fetched_at          TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL DEFAULT '',
    updated_at          TEXT NOT NULL DEFAULT '',
    UNIQUE (cid, rid, rowi)
);

CREATE TABLE IF NOT EXISTS game_players (
    game_id        INTEGER NOT NULL,
    player_order   INTEGER NOT NULL,
    seat           INTEGER,
    name           TEXT NOT NULL DEFAULT '',
    league_name    TEXT NOT NULL DEFAULT '',
    team_tid       INTEGER,
    team_name      TEXT NOT NULL DEFAULT '',
    start_score    INTEGER,
    end_score      INTEGER,
    delta          INTEGER,
    rank           INTEGER,
    is_target_team INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (game_id, player_order),
    FOREIGN KEY (game_id) REFERENCES games(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_games_round ON games(cid, round_no, rowi);
CREATE INDEX IF NOT EXISTS idx_games_status ON games(fetch_status);
CREATE INDEX IF NOT EXISTS idx_players_name ON game_players(name);
"""


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path or config.DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """给旧库补列（幂等）。"""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(games)")}
    additions = {
        "rule": "TEXT NOT NULL DEFAULT ''",
        "fetch_error": "TEXT NOT NULL DEFAULT ''",
        "fetched_at": "TEXT NOT NULL DEFAULT ''",
    }
    for name, ddl in additions.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE games ADD COLUMN {name} {ddl}")

    player_cols = {row["name"] for row in conn.execute("PRAGMA table_info(game_players)")}
    if "league_name" not in player_cols:
        conn.execute("ALTER TABLE game_players ADD COLUMN league_name TEXT NOT NULL DEFAULT ''")


def upsert_competition(conn, cid: int, name: str, rounds: int, start_points: int) -> None:
    conn.execute(
        """INSERT INTO competitions(cid, name, rounds, start_points, updated_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(cid) DO UPDATE SET
             name=excluded.name, rounds=excluded.rounds,
             start_points=excluded.start_points, updated_at=excluded.updated_at""",
        (cid, name, rounds, start_points, now()),
    )


def upsert_team(conn, cid: int, tid: int, name: str, players: str, substitutes: str) -> None:
    conn.execute(
        """INSERT INTO teams(cid, tid, name, players, substitutes)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(cid, tid) DO UPDATE SET
             name=excluded.name, players=excluded.players, substitutes=excluded.substitutes""",
        (cid, tid, name, players, substitutes),
    )


def upsert_match(conn, cid: int, rid: int, round_no: int, class_no: int,
                 code: str, tids: list[int]) -> None:
    t = (list(tids) + [0, 0, 0, 0])[:4]
    conn.execute(
        """INSERT INTO matches(cid, rid, round_no, class_no, code, team1, team2, team3, team4)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(cid, rid) DO UPDATE SET
             round_no=excluded.round_no, class_no=excluded.class_no, code=excluded.code,
             team1=excluded.team1, team2=excluded.team2, team3=excluded.team3, team4=excluded.team4""",
        (cid, rid, round_no, class_no, code, *t),
    )


def upsert_game(conn, game: dict) -> int:
    """插入/更新半庄元数据。抓取状态、本地路径、NAGA 链接不会被覆盖。"""
    stamp = now()
    conn.execute(
        """INSERT INTO games(
               cid, rid, round_no, class_no, rowi, position, position_index, position_label,
               game_date, paipu_url, paipu_uuid, start_points, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(cid, rid, rowi) DO UPDATE SET
               class_no=excluded.class_no,
               position=excluded.position,
               position_index=excluded.position_index,
               position_label=excluded.position_label,
               game_date=excluded.game_date,
               paipu_url=excluded.paipu_url,
               paipu_uuid=excluded.paipu_uuid,
               start_points=excluded.start_points,
               updated_at=excluded.updated_at""",
        (
            game["cid"], game["rid"], game["round_no"], game["class_no"], game["rowi"],
            game["position"], game["position_index"], game["position_label"],
            game["game_date"], game["paipu_url"], game["paipu_uuid"],
            game.get("start_points", 0), stamp, stamp,
        ),
    )
    row = conn.execute(
        "SELECT id FROM games WHERE cid=? AND rid=? AND rowi=?",
        (game["cid"], game["rid"], game["rowi"]),
    ).fetchone()
    return int(row["id"])


def upsert_league_players(conn, game_id: int, players: list[dict]) -> None:
    """写入赛事接口提供的四家信息（座位未知时 seat 为 NULL）。"""
    for p in players:
        conn.execute(
            """INSERT INTO game_players(
                   game_id, player_order, seat, name, league_name, team_tid, team_name,
                   start_score, end_score, delta, rank, is_target_team)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(game_id, player_order) DO UPDATE SET
                   league_name=excluded.league_name,
                   name=CASE WHEN game_players.seat IS NULL
                             THEN excluded.name ELSE game_players.name END,
                   team_tid=excluded.team_tid,
                   team_name=excluded.team_name,
                   start_score=excluded.start_score,
                   end_score=excluded.end_score,
                   delta=excluded.delta,
                   is_target_team=excluded.is_target_team,
                   seat=COALESCE(game_players.seat, excluded.seat),
                   rank=COALESCE(game_players.rank, excluded.rank)""",
            (
                game_id, p["player_order"], p.get("seat"), p["name"], p["name"],
                p.get("team_tid"), p.get("team_name", ""),
                p.get("start_score"), p.get("end_score"), p.get("delta"),
                p.get("rank"), 1 if p.get("is_target_team") else 0,
            ),
        )


def apply_tenhou(conn, game_id: int, meta: TenhouMeta, local_json_path: str) -> list[str]:
    """把牌谱里解析出的座位/点数写回数据库。返回警告。"""
    warnings = []
    rows = list(conn.execute(
        "SELECT player_order, name, team_tid, team_name, is_target_team "
        "FROM game_players WHERE game_id=? ORDER BY player_order",
        (game_id,),
    ))
    remaining = {int(r["player_order"]): r for r in rows}
    matched: dict[int, int] = {}  # seat -> player_order

    # 先按姓名精确匹配
    for seat, name in enumerate(meta.names):
        for order, row in list(remaining.items()):
            if row["name"] == name:
                matched[seat] = order
                remaining.pop(order)
                break

    # 剩下的按顺序兜底，避免出现 5~8 号“幽灵选手”
    if remaining and len(matched) < 4:
        for seat, name in enumerate(meta.names):
            if seat in matched:
                continue
            if not remaining:
                break
            order = min(remaining)
            matched[seat] = order
            remaining.pop(order)
            warnings.append(f"姓名未匹配，按顺序兜底：seat {seat} <- {order} 号位（{name}）")

    for seat, order in matched.items():
        conn.execute(
            """UPDATE game_players SET
                   seat=?, name=?, start_score=?, end_score=?, delta=?, rank=?
               WHERE game_id=? AND player_order=?""",
            (
                seat, meta.names[seat], meta.start_scores[seat], meta.end_scores[seat],
                meta.deltas[seat], meta.ranks[seat], game_id, order,
            ),
        )

    for seat, name in enumerate(meta.names):
        if seat not in matched:
            warnings.append(f"牌谱中的选手 {name} 没有写入数据库（座位 {seat}）")

    # start_points 是赛事规则的开局点数（100000），不是某一家的开局点；
    # 四家各自的开局点在 game_players.start_score。
    conn.execute(
        """UPDATE games SET
               rule=?, local_json_path=?, fetch_status='fetched',
               fetch_error='', fetched_at=?, updated_at=?
           WHERE id=?""",
        (
            str(meta.rule.get("disp", "")) if isinstance(meta.rule, dict) else "",
            local_json_path, now(), now(), game_id,
        ),
    )
    return warnings


def mark_fetch_error(conn, game_id: int, message: str) -> None:
    conn.execute(
        "UPDATE games SET fetch_status='error', fetch_error=?, updated_at=? WHERE id=?",
        (message[:2000], now(), game_id),
    )


def pending_games(conn, cid: int, limit: int | None = None,
                  round_no: int | None = None) -> list[sqlite3.Row]:
    sql = """SELECT * FROM games
             WHERE cid=? AND (fetch_status<>'fetched' OR local_json_path='')"""
    params: list[object] = [cid]
    if round_no is not None:
        sql += " AND round_no=?"
        params.append(round_no)
    sql += " ORDER BY round_no, rowi"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return list(conn.execute(sql, params))


def all_games(conn, cid: int) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM games WHERE cid=? ORDER BY round_no, rowi", (cid,)
    ))


def players_of(conn, game_id: int) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM game_players WHERE game_id=? ORDER BY seat IS NULL, seat, player_order",
        (game_id,),
    ))


def set_processed_json(conn, game_id: int, path: str) -> None:
    conn.execute(
        "UPDATE games SET processed_json_path=?, updated_at=? WHERE id=?",
        (path, now(), game_id),
    )


def set_naga_url(conn, game_id: int, url: str) -> None:
    conn.execute(
        "UPDATE games SET naga_url=?, updated_at=? WHERE id=?",
        (url, now(), game_id),
    )


def stats(conn, cid: int) -> dict:
    row = conn.execute(
        """SELECT
             COUNT(*) AS total,
             SUM(fetch_status='fetched') AS fetched,
             SUM(fetch_status='error') AS errors,
             SUM(fetch_status='pending') AS pending,
             SUM(processed_json_path<>'') AS processed,
             SUM(naga_url<>'') AS naga
           FROM games WHERE cid=?""",
        (cid,),
    ).fetchone()
    by_round = list(conn.execute(
        """SELECT round_no, COUNT(*) AS total, SUM(fetch_status='fetched') AS fetched
           FROM games WHERE cid=? GROUP BY round_no ORDER BY round_no""",
        (cid,),
    ))
    return {"total": dict(row), "by_round": by_round}
