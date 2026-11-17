"""核心逻辑的最小单元测试：python -m unittest discover -s tests -v"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import db
import league_client
import tenhou_meta


def kyoku(seed, scores, result):
    """构造一个足够让 extract() 工作的 17 元天凤局数组。"""
    return [seed, scores, [1], [], *([[]] * 12), result]


class LeagueClientTest(unittest.TestCase):
    def test_position_label(self):
        admin = {"t_type": "先鋒\r\n先鋒\r\n次鋒\r\n次鋒\r\n中堅\r\n中堅\r\n副將\r\n副將\r\n大將\r\n大將"}
        positions = league_client.parse_positions(admin)
        self.assertEqual(positions[:2], ["先锋", "先锋"])
        self.assertEqual(league_client.position_label(positions, 0), ("先锋", 1, "先锋1"))
        self.assertEqual(league_client.position_label(positions, 1), ("先锋", 2, "先锋2"))
        self.assertEqual(league_client.position_label(positions, 8), ("大将", 1, "大将1"))
        self.assertEqual(league_client.position_label(positions, 9), ("大将", 2, "大将2"))

    def test_uuid_and_date(self):
        url = "https://game.maj-soul.com/1/?paipu=261114-a22fca0f-747a-4722-9425-f4e7cebb71e8"
        self.assertEqual(league_client.paipu_uuid(url),
                         "261114-a22fca0f-747a-4722-9425-f4e7cebb71e8")
        self.assertEqual(league_client.game_date({"url": url}), "2026-11-14")

    def test_match_team_side(self):
        match = {"tid1": 1, "tid2": 2, "tid3": 3, "tid4": 4}
        self.assertEqual(league_client.match_team_side(match, {3}), 3)
        self.assertIsNone(league_client.match_team_side(match, {9}))


class TenhouMetaTest(unittest.TestCase):
    def test_extract_carry_over_scores(self):
        data = {
            "name": ["A", "B", "C", "D"],
            "sc": [30000, 5.0, 20000, -5.0, 25000, 0.0, 25000, 0.0],
            "log": [
                kyoku([0, 0, 0], [25000, 25000, 25000, 25000],
                      ["和了", [5000, -5000, 0, 0], [0, 3, 0, "30符1飜"]]),
            ],
            "title": ["四麻 半庄", "2026-11-14 12:00:00"],
            "ref": "261114-a22fca0f",
        }
        meta = tenhou_meta.extract(data)
        self.assertEqual(meta.start_scores, [25000] * 4)
        self.assertEqual(meta.end_scores, [30000, 20000, 25000, 25000])
        self.assertEqual(meta.deltas, [5000, -5000, 0, 0])
        self.assertEqual(meta.ranks, [1, 4, 2, 3])
        self.assertEqual(meta.rounds, 1)
        self.assertEqual(meta.played_at, "2026-11-14 12:00:00")

    def test_extract_fallback_end(self):
        data = {
            "name": ["A", "B", "C", "D"],
            "log": [kyoku([0, 0, 0], [25000] * 4,
                          ["和了", [3000, -1000, -1000, -1000], [0, 0, 0, "x"]])],
        }
        meta = tenhou_meta.extract(data, fallback_end=[40000, 15000, 20000, 25000])
        self.assertEqual(meta.end_scores, [40000, 15000, 20000, 25000])
        self.assertEqual(meta.deltas, [15000, -10000, -5000, 0])

    def test_validate_total_mismatch(self):
        meta = tenhou_meta.TenhouMeta(
            names=["A", "B", "C", "D"],
            start_scores=[25000] * 4,
            end_scores=[30000, 20000, 25000, 24000],
            deltas=[5000, -5000, 0, -1000],
            ranks=[1, 4, 2, 3],
            rounds=1,
        )
        warnings = tenhou_meta.validate(meta)
        self.assertTrue(any("总和" in w for w in warnings))


class DatabaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "test.db"
        self.conn = db.connect(self.path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_upsert_and_apply(self):
        db.upsert_competition(self.conn, 220, "测试杯", 1, 100000)
        db.upsert_team(self.conn, 220, 1, "立石", "甲\n乙", "")
        db.upsert_match(self.conn, 220, 10, 1, 1, "x", [1, 2, 3, 4])
        game_id = db.upsert_game(self.conn, {
            "cid": 220, "rid": 10, "round_no": 1, "class_no": 1, "rowi": 0,
            "position": "先锋", "position_index": 1, "position_label": "先锋1",
            "game_date": "2026-11-14",
            "paipu_url": "https://game.maj-soul.com/1/?paipu=261114-aaaa",
            "paipu_uuid": "261114-aaaa", "start_points": 100000,
        })
        players = []
        for i, name in enumerate(["甲", "乙", "丙", "丁"], 1):
            players.append({
                "player_order": i, "seat": None, "name": name,
                "team_tid": i, "team_name": "队伍",
                "start_score": 100000, "end_score": 100000 + i * 1000,
                "delta": i * 1000, "rank": 5 - i, "is_target_team": i == 1,
            })
        db.upsert_league_players(self.conn, game_id, players)
        self.conn.commit()

        meta = tenhou_meta.TenhouMeta(
            names=["乙", "甲", "丁", "丙"],
            start_scores=[100000] * 4,
            end_scores=[102000, 104000, 101000, 103000],
            deltas=[2000, 4000, 1000, 3000],
            ranks=[2, 1, 4, 3],
            rounds=1,
            rule={"disp": "四麻 半庄"},
        )
        db.apply_tenhou(self.conn, game_id, meta, "data/tenhou_json/261114-aaaa.json")
        self.conn.commit()

        rows = db.players_of(self.conn, game_id)
        self.assertEqual(len(rows), 4)
        by_seat = {r["seat"]: r for r in rows}
        self.assertEqual(by_seat[0]["name"], "乙")
        self.assertEqual(by_seat[0]["start_score"], 100000)
        self.assertEqual(by_seat[0]["delta"], 2000)
        self.assertEqual(by_seat[1]["name"], "甲")
        self.assertEqual(by_seat[1]["delta"], 4000)

        game = self.conn.execute("SELECT * FROM games WHERE id=?", (game_id,)).fetchone()
        self.assertEqual(game["fetch_status"], "fetched")
        self.assertEqual(game["local_json_path"], "data/tenhou_json/261114-aaaa.json")
        self.assertEqual(game["start_points"], 100000)
        self.assertEqual(game["processed_json_path"], "")
        self.assertEqual(game["naga_url"], "")

    def test_league_name_preserved_on_resync(self):
        game_id = db.upsert_game(self.conn, {
            "cid": 220, "rid": 11, "round_no": 1, "class_no": 1, "rowi": 1,
            "position": "先锋", "position_index": 2, "position_label": "先锋2",
            "game_date": "2026-11-14",
            "paipu_url": "https://game.maj-soul.com/1/?paipu=261114-bbbb",
            "paipu_uuid": "261114-bbbb", "start_points": 100000,
        })
        players = [{
            "player_order": 1, "seat": None, "name": "電腦",
            "team_tid": 1, "team_name": "立石",
            "start_score": 100000, "end_score": 90000, "delta": -10000,
            "rank": 4, "is_target_team": True,
        }]
        db.upsert_league_players(self.conn, game_id, players)
        meta = tenhou_meta.TenhouMeta(
            names=["電腦", "B", "C", "D"],
            start_scores=[100000] * 4,
            end_scores=[90000, 110000, 100000, 100000],
            deltas=[-10000, 10000, 0, 0],
            ranks=[4, 1, 2, 3],
            rounds=1,
        )
        # 手工改成游戏内昵称，模拟 apply_tenhou 的匹配结果
        self.conn.execute(
            "UPDATE game_players SET name='AI', seat=0, league_name='電腦' WHERE game_id=?",
            (game_id,),
        )
        db.upsert_league_players(self.conn, game_id, players)
        row = self.conn.execute(
            "SELECT name, league_name, seat FROM game_players WHERE game_id=?",
            (game_id,),
        ).fetchone()
        self.assertEqual(row["name"], "AI")
        self.assertEqual(row["league_name"], "電腦")
        self.assertEqual(row["seat"], 0)


if __name__ == "__main__":
    unittest.main()
