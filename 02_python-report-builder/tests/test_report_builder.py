"""python -m unittest discover -s tests"""
import csv
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import report_builder as rb  # noqa: E402

CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))


class TestParsing(unittest.TestCase):
    def test_date_variants(self):
        for v in ["2026/9/1", "2026-09-01", "20260901", "2026年9月1日", "2026/09/01 10:30", 46266]:
            self.assertEqual(rb.parse_date(v), date(2026, 9, 1), v)

    def test_invalid_date(self):
        with self.assertRaises(ValueError):
            rb.parse_date("2026/09/31")

    def test_number_variants(self):
        self.assertEqual(rb.parse_number("¥1,280"), 1280)
        self.assertEqual(rb.parse_number("￥1,280"), 1280)
        self.assertEqual(rb.parse_number("1,280円"), 1280)
        self.assertEqual(rb.parse_number("1280"), 1280)  # 全角
        self.assertEqual(rb.parse_number("(500)"), -500)
        with self.assertRaises(ValueError):
            rb.parse_number("")

    def test_previous_month(self):
        self.assertEqual(rb.previous_month("2026-01"), "2025-12")
        self.assertEqual(rb.previous_month("2026-10"), "2026-09")


class TestPipeline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write_csv(self, name, rows, encoding="utf-8-sig"):
        p = self.dir / name
        with open(p, "w", encoding=encoding, newline="") as f:
            csv.writer(f).writerows(rows)
        return p

    def test_mixed_files_and_errors(self):
        a = self.write_csv("rakuten.csv", [["注文日", "商品名", "個数", "売上金額"],
                                           ["2026/09/01", "ほうじ茶", "2", "￥2,560"],
                                           ["2026/09/31", "ほうじ茶", "1", "￥1,280"]], encoding="cp932")
        b = self.write_csv("amazon.csv", [["# 説明行"], ["order_date", "product_name", "qty", "unit_price"],
                                          ["2026-09-02", "抹茶", "3", "980"],
                                          ["2026-08-30", "抹茶", "1", "980"]])
        sales, errors = rb.load_sales([a, b], CONFIG)
        self.assertEqual(len(sales), 3)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].line, 3)
        self.assertEqual({s.channel for s in sales}, {"楽天市場", "Amazon"})

        s = rb.summarize(sales, "2026-09")
        self.assertEqual(s["total"], 2560 + 2940)
        self.assertEqual(s["prev_total"], 980)
        self.assertAlmostEqual(s["mom"], 5500 / 980 - 1)
        self.assertEqual(s["products"][0][0], "抹茶")

        out = rb.write_report(s, errors, self.dir / "out.xlsx")
        wb = load_workbook(out)
        self.assertEqual(wb.sheetnames, ["サマリー", "日別推移", "チャネル別", "商品ランキング", "明細", "エラー行"])
        self.assertEqual(wb["サマリー"]["B4"].value, 5500)
        self.assertEqual(wb["日別推移"].max_row, 31)  # ヘッダー + 30日
        self.assertEqual(wb["エラー行"].max_row, 2)

    def test_missing_header_is_reported(self):
        p = self.write_csv("bad.csv", [["a", "b"], ["1", "2"]])
        sales, errors = rb.load_sales([p], CONFIG)
        self.assertEqual(sales, [])
        self.assertIn("ヘッダー行", errors[0].reason)


if __name__ == "__main__":
    unittest.main()
