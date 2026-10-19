"""デモ用のサンプルデータを作る(形式がバラバラな3つのファイル)

    python make_sample_data.py
"""
import csv
import random
from datetime import date, timedelta
from pathlib import Path

from openpyxl import Workbook

random.seed(42)
OUT = Path(__file__).with_name("sample_data")
OUT.mkdir(exist_ok=True)
PRODUCTS = [("有機ほうじ茶 100g", 1280), ("抹茶ラテの素", 980), ("玄米茶ティーバッグ", 640),
            ("急須(常滑焼)", 4800), ("煎茶ギフトセット", 3500), ("水出し緑茶", 720)]
days = [date(2026, 8, 1) + timedelta(days=i) for i in range(61)]  # 8月〜9月


def orders(n):
    for _ in range(n):
        name, price = random.choice(PRODUCTS)
        yield random.choice(days), name, random.randint(1, 3), price


# 1. 楽天:Shift_JIS、列名は「注文日」「商品名」「個数」「売上金額」、金額は ￥1,280 表記(全角の円記号)
with open(OUT / "rakuten_orders.csv", "w", encoding="cp932", newline="") as f:
    w = csv.writer(f)
    w.writerow(["注文日", "商品名", "個数", "売上金額"])
    for d, name, q, p in orders(140):
        w.writerow([d.strftime("%Y/%m/%d"), name, q, f"￥{q * p:,}"])
    w.writerow(["2026/09/31", "抹茶ラテの素", 1, "￥980"])  # 存在しない日付(エラー行の例)

# 2. Amazon:UTF-8 BOM付き、先頭に説明行あり、列名は英語、金額なし(単価×数量で計算)
with open(OUT / "amazon_report.csv", "w", encoding="utf-8-sig", newline="") as f:
    w = csv.writer(f)
    w.writerow(["# Amazon セラーセントラル 注文レポート"])
    w.writerow(["order_date", "product_name", "qty", "unit_price"])
    for d, name, q, p in orders(110):
        w.writerow([d.isoformat(), name, q, p])
    w.writerow(["2026-09-15", "", 1, 980])  # 商品名なし(エラー行の例)

# 3. 実店舗:Excel、列名は「売上日」「店舗」「品名」「数量」「金額」、日付はExcelの日付型
wb = Workbook()
ws = wb.active
ws.append(["売上日", "店舗", "品名", "数量", "金額"])
for d, name, q, p in orders(90):
    ws.append([d, random.choice(["本店", "駅前店"]), name, q, q * p])
wb.save(OUT / "store_sales.xlsx")

print(f"サンプルデータを作りました: {OUT}")
