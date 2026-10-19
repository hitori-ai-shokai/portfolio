"""python -m unittest discover -s tests"""
import json
import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import ec_tools as et  # noqa: E402

CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
ORDERS = et.load_csv(ROOT / "sample_data" / "orders.csv")
BY_ID = {o["order_id"]: o for o in ORDERS}


def order(**kw):
    base = {"order_id": "T1", "ordered_at": "2026-10-18 10:00", "customer_name": "テスト 太郎",
            "email": "t@example.jp", "billing_pref": "東京都", "ship_pref": "東京都",
            "ship_address": "東京都千代田区1-1", "sku": "MUG-01", "qty": "1", "unit_price": "1800",
            "is_first_order": "no", "past_refunds": "0", "delivered_on": ""}
    base.update(kw)
    return base


class TestOrderCheck(unittest.TestCase):
    def levels(self):
        todays = [o for o in ORDERS if o["ordered_at"].startswith("2026-10-18")]
        return {r["order_id"]: r for r in et.check_orders(todays, CFG)}

    def test_normal_order_is_ok(self):
        r = et.check_orders([order()], CFG)[0]
        self.assertEqual((r["score"], r["level"]), (0, et.LEVEL_OK))

    def test_forwarding_high_amount_goes_to_human(self):
        r = self.levels()["A1004"]
        self.assertEqual(r["level"], et.LEVEL_REVIEW)
        self.assertEqual(r["score"], 6)  # 高額2 + 住所不一致1 + 転送2 + 使い捨て1

    def test_same_address_other_names_flags_both(self):
        lv = self.levels()
        for oid in ("A1009", "A1010"):
            self.assertEqual(lv[oid]["level"], et.LEVEL_REVIEW, oid)
            self.assertIn("別名義", lv[oid]["reasons"])

    def test_same_address_outside_window_is_ignored(self):
        a = order(order_id="X1", customer_name="A", ordered_at="2026-10-10 10:00")
        b = order(order_id="X2", customer_name="B", ordered_at="2026-10-18 10:00")
        self.assertTrue(all(r["score"] == 0 for r in et.check_orders([a, b], CFG)))

    def test_address_spacing_is_normalized(self):
        a = order(order_id="X1", customer_name="A", ship_address="東京都 千代田区1-1")
        b = order(order_id="X2", customer_name="B", ship_address="東京都千代田区1ー1")
        self.assertTrue(all(r["level"] == et.LEVEL_REVIEW for r in et.check_orders([a, b], CFG)))

    def test_caution_levels(self):
        lv = self.levels()
        self.assertEqual(lv["A1006"]["level"], et.LEVEL_CAUTION)  # 8個
        self.assertEqual(lv["A1007"]["level"], et.LEVEL_CAUTION)  # 返金2回
        self.assertEqual(lv["A1015"]["level"], et.LEVEL_REVIEW)   # 6個 + 私書箱


class TestRefund(unittest.TestCase):
    def test_unopened_within_days(self):
        ok, _ = et.check_refund(BY_ID["A0961"], date(2026, 10, 18), "no", CFG)
        self.assertTrue(ok)

    def test_opened_goes_to_human(self):
        ok, why = et.check_refund(BY_ID["A0950"], date(2026, 10, 18), "yes", CFG)
        self.assertFalse(ok)
        self.assertIn("人の確認", why)

    def test_too_late(self):
        ok, why = et.check_refund(BY_ID["A0961"], date(2026, 10, 30), "no", CFG)
        self.assertFalse(ok)
        self.assertIn("規定の8日", why)

    def test_not_delivered(self):
        ok, _ = et.check_refund(BY_ID["A1007"], date(2026, 10, 18), "yes", CFG)
        self.assertFalse(ok)
        ok, _ = et.check_refund(None, date(2026, 10, 18), "no", CFG)
        self.assertFalse(ok)


class TestDailyReport(unittest.TestCase):
    def test_totals_and_inventory_warning(self):
        todays = [o for o in ORDERS if o["ordered_at"].startswith("2026-10-18")]
        results = et.check_orders(todays, CFG)
        inv = et.load_csv(ROOT / "sample_data" / "inventory.csv")
        md = et.daily_report(ORDERS, inv, results, date(2026, 10, 18), CFG)
        total = sum(et.amount(o) for o in todays)
        self.assertIn(f"売上:{total:,}円(注文 15 件", md)
        # 当日12個売れたので、平均(1日3個)ではなく当日の数で計算する
        self.assertIn("ソイキャンドル(ラベンダー):在庫 9 個(約 0.8 日分)⚠ 発注を検討", md)
        self.assertIn("真鍮テーブルランプ:在庫 3 個(約 1.0 日分)⚠ 発注を検討", md)
        self.assertIn("帆布トートバッグ:在庫 15 個(約 3.8 日分)⚠ 発注を検討", md)
        self.assertIn("波佐見焼プレート:在庫 80 個(約 13.3 日分)OK", md)


if __name__ == "__main__":
    unittest.main()
