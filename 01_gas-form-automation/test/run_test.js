// Code.gs のロジック部分を Node.js で単体テストする(node test/run_test.js)
const fs = require('fs');
const path = require('path');
const assert = require('assert');
const Module = require('module');

const src = fs.readFileSync(path.join(__dirname, '..', 'Code.gs'), 'utf8');
const m = new Module('Code.gs');
m._compile(src, 'Code.gs');
const t = m.exports;

let passed = 0;
const test = (name, fn) => { fn(); passed++; console.log('ok -', name); };

const settings = {
  '対応日数': '3', '既定の担当者': '未割当',
  '列名:氏名': 'お名前', '列名:メール': 'メールアドレス', '列名:種別': 'お問い合わせの種類', '列名:内容': 'お問い合わせ内容',
};

test('フォームの列名の違いを吸収する', () => {
  const a = t.normalizeAnswer_({ 'お名前': [' 山田 太郎 '], 'メールアドレス': ['taro@example.com'], 'お問い合わせの種類': ['見積もり'], 'お問い合わせ内容': ['50部お願いします'] }, settings);
  assert.deepStrictEqual(a, { 氏名: '山田 太郎', メール: 'taro@example.com', 種別: '見積もり', 内容: '50部お願いします' });
});

test('種別に応じて担当者を振り分け、未登録の種別は既定の担当者にする', () => {
  const routing = { 見積もり: '営業 佐藤' };
  const now = new Date(2026, 9, 16); // 金曜
  const r1 = t.buildRecord_({ 氏名: 'A', メール: 'a@x', 種別: '見積もり', 内容: '' }, settings, routing, now, 1);
  const r2 = t.buildRecord_({ 氏名: 'B', メール: 'b@x', 種別: 'その他', 内容: '' }, settings, routing, now, 2);
  assert.strictEqual(r1['担当者'], '営業 佐藤');
  assert.strictEqual(r2['担当者'], '未割当');
  assert.strictEqual(r1['受付番号'], '20261016-001');
  assert.strictEqual(r1['ステータス'], '未対応');
});

test('対応期限は土日を除いた営業日で計算する', () => {
  // 2026-10-16(金)の3営業日後 → 10-21(水)
  assert.strictEqual(t.formatDate_(t.addBusinessDays_(new Date(2026, 9, 16), 3)), '2026-10-21');
  // 2026-10-17(土)の1営業日後 → 10-19(月)
  assert.strictEqual(t.formatDate_(t.addBusinessDays_(new Date(2026, 9, 17), 1)), '2026-10-19');
});

test('テンプレートの {キー} を置き換え、未知のキーはそのまま残す', () => {
  const s = t.fillTemplate_('{氏名} 様 / 期限 {対応期限} / {不明}', { 氏名: '山田', 対応期限: '2026-10-21' });
  assert.strictEqual(s, '山田 様 / 期限 2026-10-21 / {不明}');
});

test('未対応・期限切れ・担当者別の件数を集計する', () => {
  const rows = [
    { 受付番号: '1', 種別: '見積もり', 担当者: '佐藤', ステータス: '未対応', 対応期限: '2026-10-10' },
    { 受付番号: '2', 種別: '不具合', 担当者: '鈴木', ステータス: '対応中', 対応期限: '2026-10-30' },
    { 受付番号: '3', 種別: '見積もり', 担当者: '佐藤', ステータス: '完了', 対応期限: '2026-10-01' },
  ];
  const s = t.summarize_(rows, new Date(2026, 9, 16));
  assert.strictEqual(s.open, 2);
  assert.deepStrictEqual(s.overdue.map((r) => r['受付番号']), ['1']);
  assert.deepStrictEqual(s.byAssignee, { 佐藤: 1, 鈴木: 1 });
  assert.ok(t.formatSummary_(s).includes('#1 見積もり'));
});

console.log(`\n${passed} tests passed`);
