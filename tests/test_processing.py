"""本地 JSON 规则的转换逻辑测试。"""

from __future__ import annotations

import unittest

import process_pairs


def kyoku(seed, scores, result):
    return [seed, scores, [1], [], *([[]] * 12), result]


def make_data(names, starts, finals):
    return {
        "name": names,
        "sc": sum(([finals[i], (finals[i] - 100000) / 1000] for i in range(4)), []),
        "log": [
            kyoku([0, 0, 0], list(starts), ["和了", [0, 0, 0, 0], [0, 0, 0, ""]]),
            kyoku([1, 0, 0], [s + 1000 for s in starts], ["流局", [0, 0, 0, 0]]),
        ],
        "title": ["四麻 半庄", "2026-11-14 12:00:00"],
        "rule": {"disp": "四麻 半庄"},
    }


class ResetX1Test(unittest.TestCase):
    def test_all_rounds_start_100000(self):
        data = make_data(["A", "B", "C", "D"],
                         [90000, 110000, 105000, 95000],
                         [95000, 105000, 110000, 90000])
        out = process_pairs.reset_x1(data)
        for kyoku in out["log"]:
            self.assertEqual(kyoku[1], [100000] * 4)
        # 其他字段不变
        self.assertEqual(out["name"], data["name"])
        self.assertEqual(out["sc"], data["sc"])

    def test_original_not_mutated(self):
        data = make_data(["A", "B", "C", "D"], [90000] * 4, [100000] * 4)
        before = [list(k[1]) for k in data["log"]]
        process_pairs.reset_x1(data)
        self.assertEqual([k[1] for k in data["log"]], before)


class MapX2Test(unittest.TestCase):
    def test_name_then_score_for_substitute(self):
        x1_info = [
            {"seat": 0, "name": "A", "start": 100000, "final": 110000, "net": 10000},
            {"seat": 1, "name": "B", "start": 100000, "final": 90000, "net": -10000},
            {"seat": 2, "name": "C", "start": 100000, "final": 105000, "net": 5000},
            {"seat": 3, "name": "D", "start": 100000, "final": 95000, "net": -5000},
        ]
        # X2 座位轮转；E 替补了 B，继承了 B 的终局 90000
        mapping, methods = process_pairs.map_x2_to_x1(
            x1_info, ["C", "A", "E", "D"], [105000, 110000, 90000, 95000]
        )
        self.assertEqual(mapping[0]["seat"], 2)
        self.assertEqual(mapping[1]["seat"], 0)
        self.assertEqual(mapping[2]["seat"], 1)
        self.assertEqual(mapping[3]["seat"], 3)
        self.assertEqual(methods[0], "name")
        self.assertEqual(methods[2], "score")


class OffsetX2Test(unittest.TestCase):
    def test_offset_constant_and_first_round(self):
        x1_info = [
            {"seat": 0, "name": "A", "start": 100000, "final": 110000, "net": 10000},
            {"seat": 1, "name": "B", "start": 100000, "final": 90000, "net": -10000},
            {"seat": 2, "name": "C", "start": 100000, "final": 105000, "net": 5000},
            {"seat": 3, "name": "D", "start": 100000, "final": 95000, "net": -5000},
        ]
        # X1 实际首局不是 100000 的情形：原 X2 首局 = X1 终局
        x2 = make_data(["C", "A", "B", "D"],
                       [105000, 110000, 90000, 95000],
                       [100000, 115000, 85000, 100000])
        mapping, _ = process_pairs.map_x2_to_x1(x1_info, x2["name"], x2["log"][0][1])
        out, new_start, offset = process_pairs.offset_x2(x2, mapping)
        self.assertEqual([new_start[i] for i in range(4)], [105000, 110000, 90000, 95000])
        self.assertEqual([offset[i] for i in range(4)], [0, 0, 0, 0])
        for orig, proc in zip(x2["log"], out["log"]):
            self.assertEqual([proc[1][i] - orig[1][i] for i in range(4)], [0, 0, 0, 0])

    def test_nonzero_offset(self):
        x1_info = [
            {"seat": 0, "name": "A", "start": 90000, "final": 100000, "net": 10000},
            {"seat": 1, "name": "B", "start": 90000, "final": 80000, "net": -10000},
            {"seat": 2, "name": "C", "start": 90000, "final": 95000, "net": 5000},
            {"seat": 3, "name": "D", "start": 90000, "final": 85000, "net": -5000},
        ]
        x2 = make_data(["A", "B", "C", "D"],
                       [100000, 80000, 95000, 85000],
                       [105000, 75000, 100000, 90000])
        mapping, _ = process_pairs.map_x2_to_x1(x1_info, x2["name"], x2["log"][0][1])
        out, new_start, offset = process_pairs.offset_x2(x2, mapping)
        self.assertEqual([new_start[i] for i in range(4)], [110000, 90000, 105000, 95000])
        self.assertEqual([offset[i] for i in range(4)], [10000, 10000, 10000, 10000])
        for orig, proc in zip(x2["log"], out["log"]):
            self.assertEqual([proc[1][i] - orig[1][i] for i in range(4)], [10000] * 4)


if __name__ == "__main__":
    unittest.main()
