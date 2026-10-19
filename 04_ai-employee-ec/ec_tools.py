"""EC運営の「AI社員」が使う道具(注文チェック・返品の規定判定・日次レポート)。

判断に迷うもの(不正の疑い・規定外の返品)は、AIが決めずに「人の確認待ち」に回す。
使い方:
    python ec_tools.py --date 2026-10-18
"""
import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LEVEL_REVIEW, LEVEL_CAUTION, LEVEL_OK = "要確認(人へ)", "注意", "問題なし"


def load_csv(path):
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def parse_dt(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M")


def amount(order):
    return int(order["qty"]) * int(order["unit_price"])


def normalize_address(s):
    return "".join(s.split()).replace("ー", "-").replace("−", "-")


def check_orders(orders, cfg):
    """注文ごとに点数と理由をつける。点数が高いものは人の確認待ちにする。"""
    c, pts = cfg["order_check"], cfg["order_check"]["points"]
    reasons = defaultdict(list)

    # 同じ配送先に、別の名義の注文が短時間に集中していないか
    by_addr = defaultdict(list)
    for o in orders:
        by_addr[normalize_address(o["ship_address"])].append(o)
    for group in by_addr.values():
        group.sort(key=lambda o: parse_dt(o["ordered_at"]))
        for o in group:
            t = parse_dt(o["ordered_at"])
            near = [g for g in group
                    if abs((parse_dt(g["ordered_at"]) - t).total_seconds()) <= c["same_address_window_hours"] * 3600]
            if len({g["customer_name"] for g in near}) >= 2:
                others = sorted(g["order_id"] for g in near if g["order_id"] != o["order_id"])
                reasons[o["order_id"]].append(
                    (pts["same_address_other_names"], f"同じ配送先に別名義の注文がある({', '.join(others)})"))

    for o in orders:
        r = reasons[o["order_id"]]
        if o["is_first_order"] == "yes" and amount(o) >= c["high_amount_first_order"]:
            r.append((pts["high_amount_first_order"], f"初めての注文で高額({amount(o):,}円)"))
        if o["billing_pref"] != o["ship_pref"]:
            r.append((pts["billing_ship_mismatch"], f"請求先({o['billing_pref']})と配送先({o['ship_pref']})が違う"))
        if int(o["qty"]) >= c["bulk_qty"]:
            r.append((pts["bulk_qty"], f"同じ商品を{o['qty']}個"))
        if int(o["past_refunds"]) >= c["repeat_refunds"]:
            r.append((pts["repeat_refunds"], f"過去の返金が{o['past_refunds']}回"))
        words = [w for w in c["suspicious_address_words"] if w in o["ship_address"]]
        if words:
            r.append((pts["suspicious_address"], f"配送先に「{'・'.join(words)}」を含む"))
        if o["email"].split("@")[-1].lower() in c["disposable_email_domains"]:
            r.append((pts["disposable_email"], "使い捨てメールのドメイン"))

    results = []
    for o in orders:
        r = reasons[o["order_id"]]
        score = sum(p for p, _ in r)
        level = (LEVEL_REVIEW if score >= c["review_threshold"]
                 else LEVEL_CAUTION if score >= c["caution_threshold"] else LEVEL_OK)
        results.append({"order_id": o["order_id"], "score": score, "level": level,
                         "reasons": " / ".join(t for _, t in r)})
    return results


def check_refund(order, requested_on, opened, cfg):
    """返品が規定の範囲内かを判定する。範囲外・判断できないものは人に回す。"""
    pol = cfg["refund_policy"]
    if not order or not order.get("delivered_on"):
        return False, "未配達、または注文が見つからないため判定できない(人の確認へ)"
    days = (requested_on - date.fromisoformat(order["delivered_on"])).days
    if days > pol["days_from_delivery"]:
        return False, f"お届けから{days}日で、規定の{pol['days_from_delivery']}日を過ぎている(人の確認へ)"
    if pol["unopened_only"] and opened != "no":
        return False, "開封済み、または開封の有無が不明(不良品の可能性もあるため人の確認へ)"
    return True, f"お届けから{days}日・未開封で、規定の範囲内"


def daily_report(orders, inventory, results, day, cfg):
    todays = [o for o in orders if parse_dt(o["ordered_at"]).date() == day]
    sales = sum(amount(o) for o in todays)
    by_sku, qty_sku = Counter(), Counter()
    for o in todays:
        by_sku[o["sku"]] += amount(o)
        qty_sku[o["sku"]] += int(o["qty"])
    names = {i["sku"]: i["name"] for i in inventory}
    ids = {o["order_id"] for o in todays}
    review = [r for r in results if r["order_id"] in ids and r["level"] == LEVEL_REVIEW]

    lines = [f"# 日次レポート {day.isoformat()}({cfg['shop_name']})", "",
             f"- 売上:{sales:,}円(注文 {len(todays)} 件、平均 {sales // max(len(todays), 1):,}円)",
             f"- 人の確認待ちの注文:{len(review)} 件", "", "## 商品別の売上"]
    for sku, v in by_sku.most_common():
        lines.append(f"- {names.get(sku, sku)}:{v:,}円")
    lines += ["", "## 在庫の注意(在庫が持つ日数が少ない順。平均と当日の売れ数の多いほうで計算)"]
    warn = cfg["inventory"]["warn_days_of_stock"]
    rows = sorted(((int(i["stock"]) / max(float(i["avg_daily_sales"]), qty_sku[i["sku"]]), i) for i in inventory),
                  key=lambda x: x[0])
    for d, i in rows:
        mark = "⚠ 発注を検討" if d < warn else "OK"
        lines.append(f"- {i['name']}:在庫 {i['stock']} 個(約 {d:.1f} 日分){mark}")
    if review:
        lines += ["", "## 人の確認待ち"]
        for r in review:
            lines.append(f"- {r['order_id']}({r['score']}点):{r['reasons']}")
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--data", default=str(ROOT / "sample_data"))
    ap.add_argument("--out", default=str(ROOT / "output"))
    a = ap.parse_args()
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    day = date.fromisoformat(a.date)
    data, out = Path(a.data), Path(a.out)
    out.mkdir(exist_ok=True)

    orders = load_csv(data / "orders.csv")
    todays = [o for o in orders if parse_dt(o["ordered_at"]).date() == day]
    results = check_orders(todays, cfg)
    with open(out / f"注文チェック_{a.date}.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["order_id", "score", "level", "reasons"])
        w.writeheader()
        w.writerows(sorted(results, key=lambda r: -r["score"]))

    by_id = {o["order_id"]: o for o in orders}
    with open(out / f"返品判定_{a.date}.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["inquiry_id", "order_id", "規定内か", "理由"])
        for q in load_csv(data / "inquiries.csv"):
            if "返品" in q["body"] or "返金" in q["body"]:
                ok, why = check_refund(by_id.get(q["order_id"]), parse_dt(q["received_at"]).date(), q["opened"], cfg)
                w.writerow([q["inquiry_id"], q["order_id"], "はい" if ok else "いいえ", why])

    inventory = load_csv(data / "inventory.csv")
    (out / f"日次レポート_{a.date}.md").write_text(daily_report(orders, inventory, results, day, cfg), encoding="utf-8")
    print(f"出力しました:{out}")


if __name__ == "__main__":
    main()
