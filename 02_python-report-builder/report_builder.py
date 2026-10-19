"""月次売上レポート 自動作成ツール

複数のCSV・Excelファイル(店舗・ECモールごとに形式がバラバラ)を読み込み、
1つの月次レポート(Excel)にまとめます。

    python report_builder.py --input sample_data --month 2026-09 --out output/report_2026-09.xlsx

対応していること
- 文字コードの自動判別(UTF-8 / UTF-8 BOM付き / Shift_JIS)
- 列名のゆれ(「日付」「注文日」など)を config.json で吸収
- 金額の表記ゆれ(「¥1,200」「1200円」など)、日付の表記ゆれ(2026/9/1、2026-09-01、20260901、Excelの日付)
- 読めなかった行は捨てずに「エラー行」シートへ理由つきで出力
- 前月のデータがあれば前月比を計算
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.chart import BarChart, LineChart, Reference
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

ENCODINGS = ("utf-8-sig", "cp932")
HEADER_FILL = PatternFill("solid", fgColor="1F2933")
HEADER_FONT = Font(color="FFFFFF", bold=True)
THIN = Side(style="thin", color="C8CDD3")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
YEN = '"¥"#,##0'


@dataclass
class Sale:
    date: date
    channel: str
    product: str
    quantity: float
    amount: int
    source: str


@dataclass
class RowError:
    source: str
    line: int
    reason: str
    raw: str


# ---------------------------------------------------------------------------
# 値の変換(表記ゆれの吸収)
# ---------------------------------------------------------------------------

def parse_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) and 20000 < value < 80000:  # Excelのシリアル値
        return date(1899, 12, 30) + timedelta(days=int(value))
    s = str(value).strip()
    s = re.sub(r"[年月]", "/", s).replace("日", "")
    for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%Y%m%d", "%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"日付として読めません: {value!r}")


def parse_number(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    s = s.translate(str.maketrans("0123456789,.-", "0123456789,.-"))  # 全角数字を半角に
    s = re.sub(r"[¥￥,円\s]", "", s)
    if s in ("", "-"):
        raise ValueError("空欄です")
    if s.startswith("(") and s.endswith(")"):  # 会計表記のマイナス (1,200)
        s = "-" + s[1:-1]
    return float(s)


# ---------------------------------------------------------------------------
# 読み込み
# ---------------------------------------------------------------------------

def read_table(path: Path) -> list[list]:
    """CSV・Excelを、ヘッダー行を含む2次元リストとして返す"""
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        ws = load_workbook(path, read_only=True, data_only=True).active
        return [list(r) for r in ws.iter_rows(values_only=True)]
    data = path.read_bytes()
    for enc in ENCODINGS:
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError(f"{path.name}: 文字コードを判別できません")
    return [row for row in csv.reader(io.StringIO(text))]


def map_columns(header: list, aliases: dict[str, list[str]]) -> dict[str, int]:
    """ヘッダー行から、標準の項目名 → 列番号 の対応を作る"""
    normalized = [str(h or "").strip().lower() for h in header]
    mapping = {}
    for key, names in aliases.items():
        for name in names:
            if name.lower() in normalized:
                mapping[key] = normalized.index(name.lower())
                break
    return mapping


def find_header_row(rows: list[list], aliases: dict[str, list[str]]) -> int:
    """先頭の説明行などを飛ばして、日付列と商品列が見つかる行をヘッダーとみなす"""
    for i, row in enumerate(rows[:10]):
        m = map_columns(row, aliases)
        if "date" in m and "product" in m:
            return i
    raise ValueError("ヘッダー行(日付・商品名の列)が見つかりません")


def channel_for(path: Path, config: dict) -> str:
    name = path.stem.lower()
    for word, channel in config.get("channel_from_filename", {}).items():
        if not word.startswith("_") and word in name:
            return channel
    return path.stem


def load_sales(paths: list[Path], config: dict) -> tuple[list[Sale], list[RowError]]:
    aliases = config["columns"]
    sales, errors = [], []
    for path in paths:
        try:
            rows = read_table(path)
            h = find_header_row(rows, aliases)
        except ValueError as e:
            errors.append(RowError(path.name, 0, str(e), ""))
            continue
        cols = map_columns(rows[h], aliases)
        default_channel = channel_for(path, config)
        for line, row in enumerate(rows[h + 1:], start=h + 2):
            if not any(str(c or "").strip() for c in row):
                continue  # 空行
            get = lambda k: row[cols[k]] if k in cols and cols[k] < len(row) else None
            try:
                qty = parse_number(get("quantity")) if get("quantity") not in (None, "") else 1.0
                if get("amount") not in (None, ""):
                    amount = parse_number(get("amount"))
                elif get("unit_price") not in (None, ""):
                    amount = parse_number(get("unit_price")) * qty
                else:
                    raise ValueError("金額も単価もありません")
                product = str(get("product") or "").strip()
                if not product:
                    raise ValueError("商品名が空です")
                sales.append(Sale(
                    date=parse_date(get("date")),
                    channel=str(get("channel") or "").strip() or default_channel,
                    product=product,
                    quantity=qty,
                    amount=round(amount),
                    source=path.name,
                ))
            except (ValueError, TypeError) as e:
                errors.append(RowError(path.name, line, str(e), ", ".join(str(c) for c in row)))
    return sales, errors


# ---------------------------------------------------------------------------
# 集計
# ---------------------------------------------------------------------------

def month_range(month: str) -> tuple[date, date]:
    y, m = map(int, month.split("-"))
    start = date(y, m, 1)
    end = date(y + (m == 12), m % 12 + 1, 1)
    return start, end


def previous_month(month: str) -> str:
    y, m = map(int, month.split("-"))
    return f"{y - (m == 1)}-{(m - 2) % 12 + 1:02d}"


def summarize(sales: list[Sale], month: str, top_n: int = 10) -> dict:
    start, end = month_range(month)
    cur = [s for s in sales if start <= s.date < end]
    p_start, p_end = month_range(previous_month(month))
    prev = [s for s in sales if p_start <= s.date < p_end]

    daily = defaultdict(int)
    for s in cur:
        daily[s.date] += s.amount
    days = [start + timedelta(days=i) for i in range((end - start).days)]

    by_channel = defaultdict(lambda: [0, 0])
    by_product = defaultdict(lambda: [0, 0.0])
    for s in cur:
        by_channel[s.channel][0] += s.amount
        by_channel[s.channel][1] += 1
        by_product[s.product][0] += s.amount
        by_product[s.product][1] += s.quantity

    total, prev_total = sum(s.amount for s in cur), sum(s.amount for s in prev)
    return {
        "month": month,
        "total": total,
        "count": len(cur),
        "avg": round(total / len(cur)) if cur else 0,
        "prev_total": prev_total,
        "mom": (total / prev_total - 1) if prev_total else None,
        "daily": [(d, daily.get(d, 0)) for d in days],
        "channels": sorted(by_channel.items(), key=lambda kv: -kv[1][0]),
        "products": sorted(by_product.items(), key=lambda kv: -kv[1][0])[:top_n],
        "rows": sorted(cur, key=lambda s: (s.date, s.channel)),
    }


# ---------------------------------------------------------------------------
# Excel出力
# ---------------------------------------------------------------------------

def _table(ws, top_left_row: int, header: list[str], rows: list[list], formats: dict[int, str] | None = None):
    for j, h in enumerate(header, start=1):
        c = ws.cell(top_left_row, j, h)
        c.fill, c.font, c.border, c.alignment = HEADER_FILL, HEADER_FONT, BORDER, Alignment(horizontal="center")
    for i, row in enumerate(rows, start=top_left_row + 1):
        for j, v in enumerate(row, start=1):
            c = ws.cell(i, j, v)
            c.border = BORDER
            if formats and j in formats:
                c.number_format = formats[j]
    for j in range(1, len(header) + 1):
        width = max(len(str(ws.cell(r, j).value or "")) for r in range(top_left_row, top_left_row + len(rows) + 1))
        ws.column_dimensions[get_column_letter(j)].width = min(max(10, width * 1.8), 48)


def write_report(summary: dict, errors: list[RowError], out: Path) -> Path:
    wb = Workbook()

    # サマリー
    ws = wb.active
    ws.title = "サマリー"
    ws["A1"] = f"{summary['month']} 月次売上レポート"
    ws["A1"].font = Font(size=16, bold=True)
    kpis = [
        ["売上合計", summary["total"]],
        ["取引件数", summary["count"]],
        ["平均単価", summary["avg"]],
        ["前月の売上", summary["prev_total"] or "データなし"],
        ["前月比", summary["mom"] if summary["mom"] is not None else "データなし"],
        ["読み込めなかった行", len(errors)],
    ]
    _table(ws, 3, ["指標", "値"], kpis)
    for r in (4, 5, 6, 7):
        ws.cell(r, 2).number_format = YEN if r != 5 else "#,##0"
    if summary["mom"] is not None:
        ws.cell(8, 2).number_format = "+0.0%;-0.0%"
    ws.column_dimensions["A"].width = 22
    ws.column_dimensions["B"].width = 18

    # 日別推移
    wd = wb.create_sheet("日別推移")
    _table(wd, 1, ["日付", "売上"], [[d, v] for d, v in summary["daily"]], {1: "yyyy/mm/dd", 2: YEN})
    chart = LineChart()
    chart.title, chart.height, chart.width = "日別売上", 8, 18
    chart.add_data(Reference(wd, min_col=2, min_row=1, max_row=len(summary["daily"]) + 1), titles_from_data=True)
    chart.set_categories(Reference(wd, min_col=1, min_row=2, max_row=len(summary["daily"]) + 1))
    chart.x_axis.number_format = "m/d"
    wd.add_chart(chart, "D2")

    # チャネル別
    wc = wb.create_sheet("チャネル別")
    total = summary["total"] or 1
    _table(wc, 1, ["チャネル", "売上", "件数", "構成比"],
           [[k, v[0], v[1], v[0] / total] for k, v in summary["channels"]], {2: YEN, 3: "#,##0", 4: "0.0%"})
    if summary["channels"]:
        bar = BarChart()
        bar.title, bar.height, bar.width = "チャネル別売上", 8, 14
        bar.add_data(Reference(wc, min_col=2, min_row=1, max_row=len(summary["channels"]) + 1), titles_from_data=True)
        bar.set_categories(Reference(wc, min_col=1, min_row=2, max_row=len(summary["channels"]) + 1))
        bar.legend = None
        wc.add_chart(bar, "F2")

    # 商品ランキング
    wp = wb.create_sheet("商品ランキング")
    _table(wp, 1, ["順位", "商品名", "売上", "数量"],
           [[i, k, v[0], v[1]] for i, (k, v) in enumerate(summary["products"], start=1)], {3: YEN, 4: "#,##0"})

    # 明細(統合データ)
    wm = wb.create_sheet("明細")
    _table(wm, 1, ["日付", "チャネル", "商品名", "数量", "金額", "元ファイル"],
           [[s.date, s.channel, s.product, s.quantity, s.amount, s.source] for s in summary["rows"]],
           {1: "yyyy/mm/dd", 4: "#,##0", 5: YEN})
    wm.auto_filter.ref = wm.dimensions
    wm.freeze_panes = "A2"

    # エラー行
    we = wb.create_sheet("エラー行")
    _table(we, 1, ["ファイル", "行", "理由", "元データ"], [[e.source, e.line, e.reason, e.raw] for e in errors])

    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="複数の売上ファイルから月次レポート(Excel)を作ります")
    ap.add_argument("--input", required=True, help="CSV・Excelを置いたフォルダ")
    ap.add_argument("--month", required=True, help="対象の月(例: 2026-09)")
    ap.add_argument("--out", help="出力先(省略時: output/report_<月>.xlsx)")
    ap.add_argument("--config", default=str(Path(__file__).with_name("config.json")))
    args = ap.parse_args(argv)

    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", args.month):
        ap.error("--month は 2026-09 の形式で指定してください")
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    paths = sorted(p for p in Path(args.input).iterdir() if p.suffix.lower() in (".csv", ".xlsx", ".xlsm") and not p.name.startswith("~$"))
    if not paths:
        print(f"{args.input} にCSV・Excelがありません", file=sys.stderr)
        return 1

    sales, errors = load_sales(paths, config)
    summary = summarize(sales, args.month, config.get("top_n_products", 10))
    out = write_report(summary, errors, Path(args.out or f"output/report_{args.month}.xlsx"))

    print(f"読み込み: {len(paths)}ファイル / {len(sales)}行(エラー {len(errors)}行)")
    print(f"{args.month} の売上合計: ¥{summary['total']:,}({summary['count']}件)")
    if summary["mom"] is not None:
        print(f"前月比: {summary['mom']:+.1%}")
    print(f"出力: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
