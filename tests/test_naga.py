"""ricochet NAGA 报告匹配与 URL 构造的单元测试（不联网）。"""

from __future__ import annotations

import unittest

import naga_client


def account(reports):
    return {
        "status": 1,
        "data": {
            "accounts": [{
                "id": 12345, "username": "tester", "is_main": True, "left_np": 3640,
                "reports": {"report": reports},
            }]
        },
    }


def report(raw, players, rid, ptypes="2,4"):
    return {
        "data": [raw, [[p, 0] for p in players], rid, 0, [2, 2, 0, ptypes], 0],
        "is_collected": False,
        "mortal_key": "",
        "notes": [],
    }


class NagaTest(unittest.TestCase):
    def test_report_fields(self):
        e = report("custom_haihu_x", ["A", "B", "C", "D"], "abcv2_2")
        raw, players, rid, ptypes = naga_client.report_fields(e)
        self.assertEqual(raw, "custom_haihu_x")
        self.assertEqual(players, {"A", "B", "C", "D"})
        self.assertEqual(rid, "abcv2_2")
        self.assertEqual(ptypes, "2,4")

    def test_find_report_prefers_new(self):
        old = report("custom_haihu_old", ["A", "B", "C", "D"], "oldv2_2")
        new = report("custom_haihu_new", ["A", "B", "C", "D"], "newv2_2")
        acc = account([old, new])
        found = naga_client.find_report(acc, {"A", "B", "C", "D"},
                                        {"custom_haihu_old"})
        self.assertEqual(naga_client.report_fields(found)[2], "newv2_2")

    def test_find_report_three_of_four(self):
        e = report("custom_haihu_x", ["A", "B", "C", "X"], "abcv2_2")
        found = naga_client.find_report(account([e]), {"A", "B", "C", "D"})
        self.assertIsNotNone(found)

    def test_report_urls(self):
        proxy, direct = naga_client.report_urls("abcv2_2")
        self.assertIn("report_id=abcv2_2", proxy)
        self.assertTrue(proxy.startswith("https://ricochet.cn/api/naga/proxy/"))
        self.assertTrue(direct.startswith("https://naga.dmv.nico/htmls/"))

    def test_main_account(self):
        acc = naga_client.main_account(account([]))
        self.assertEqual(acc["id"], 12345)

    def test_normalize_all_noten_and_first_round(self):
        data = {"log": [
            [[0, 0, 0], [86700, 89700, 114100, 109500], [], [], *([[]] * 12),
             ["流局", [0, 0, 0, 0]]],
            [[1, 0, 0], [82800, 87700, 112100, 117400], [], [], *([[]] * 12),
             ["流局", [1500, -1500, 0, 0]]],
            [[2, 0, 0], [82800, 91600, 111100, 114500], [], [], *([[]] * 12),
             ["和了", [0, 0, 0, 0], []]],
        ]}
        out = naga_client.normalize_for_naga(data, starts_mode="first")
        # 只改首局开局为 100000
        self.assertEqual(out["log"][0][1], [100000] * 4)
        # 后续局保持原开局
        self.assertEqual(out["log"][1][1], [82800, 87700, 112100, 117400])
        self.assertEqual(out["log"][2][1], [82800, 91600, 111100, 114500])
        self.assertEqual(out["log"][0][-1], ["全員不聴", [0, 0, 0, 0]])
        self.assertEqual(out["log"][1][-1], ["流局", [1500, -1500, 0, 0]])
        self.assertEqual(out["log"][2][-1][0], "和了")
        # 原数据不被修改
        self.assertEqual(data["log"][0][1], [86700, 89700, 114100, 109500])
        self.assertEqual(data["log"][0][-1], ["流局", [0, 0, 0, 0]])

    def test_normalize_shift(self):
        data = {"log": [
            [[0, 0, 0], [86700, 89700, 114100, 109500], [], [], *([[]] * 12),
             ["和了", [1, 2, 3, -6], []]],
            [[1, 0, 0], [82800, 87700, 112100, 117400], [], [], *([[]] * 12),
             ["和了", [1, 2, 3, -6], []]],
            [[2, 0, 0], [82800, 91600, 111100, 114500], [], [], *([[]] * 12),
             ["和了", [1, 2, 3, -6], []]],
        ]}
        out = naga_client.normalize_for_naga(data, starts_mode="shift")
        self.assertEqual(out["log"][0][1], [100000] * 4)
        self.assertEqual(out["log"][1][1], [96100, 98000, 98000, 107900])
        self.assertEqual(out["log"][2][1], [96100, 101900, 97000, 105000])
        # 局间差保持不变
        self.assertEqual(
            [out["log"][1][1][i] - out["log"][0][1][i] for i in range(4)],
            [-3900, -2000, -2000, 7900],
        )

    def test_normalize_shift25(self):
        data = {"log": [
            [[0, 0, 0], [86700, 89700, 114100, 109500], [], [], *([[]] * 12),
             ["和了", [1, 2, 3, -6], []]],
            [[1, 0, 0], [82800, 87700, 112100, 117400], [], [], *([[]] * 12),
             ["和了", [1, 2, 3, -6], []]],
        ]}
        out = naga_client.normalize_for_naga(data, starts_mode="shift25")
        self.assertEqual(out["log"][0][1], [25000] * 4)
        self.assertEqual(out["log"][1][1], [21100, 23000, 23000, 32900])
        self.assertEqual(sum(out["log"][1][1]), 100000)

    def test_normalize_shift75(self):
        data = {"log": [
            [[0, 0, 0], [86700, 89700, 114100, 109500], [], [], *([[]] * 12),
             ["和了", [1, 2, 3, -6], []]],
            [[1, 0, 0], [82800, 87700, 112100, 117400], [], [], *([[]] * 12),
             ["和了", [1, 2, 3, -6], []]],
        ]}
        out = naga_client.normalize_for_naga(data, starts_mode="shift75")
        self.assertEqual(out["log"][0][1], [11700, 14700, 39100, 34500])
        self.assertEqual(sum(out["log"][0][1]), 100000)
        self.assertEqual(out["log"][1][1], [7800, 12700, 37100, 42400])
        self.assertEqual(sum(out["log"][1][1]), 100000)

    def test_normalize_all_starts(self):
        data = {"log": [
            [[0, 0, 0], [86700, 89700, 114100, 109500], [], [], *([[]] * 12),
             ["和了", [1, 2, 3, -6], []]],
            [[1, 0, 0], [82800, 87700, 112100, 117400], [], [], *([[]] * 12),
             ["和了", [1, 2, 3, -6], []]],
        ]}
        out = naga_client.normalize_for_naga(data, starts_mode="all")
        self.assertEqual(out["log"][0][1], [100000] * 4)
        self.assertEqual(out["log"][1][1], [100000] * 4)
        out2 = naga_client.normalize_for_naga(data, starts_mode="none")
        self.assertEqual(out2["log"][0][1], [86700, 89700, 114100, 109500])

    def test_estimate_cost(self):
        self.assertEqual(naga_client.estimate_cost(11, "2,4"), 110)
        self.assertEqual(naga_client.estimate_cost(13, "2,4,6"), 143)
        self.assertEqual(naga_client.estimate_cost(1, "2"), 10)
        self.assertEqual(naga_client.estimate_cost(1, ""), 10)


if __name__ == "__main__":
    unittest.main()
