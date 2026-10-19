/**
 * フォーム回答 自動処理ツール(Google Apps Script)
 *
 * Googleフォームの回答が届いたら、次の処理を自動で行います。
 *   1. 「管理」シートに受付番号・担当者・ステータスを付けて転記
 *   2. 回答者へ自動返信メールを送信
 *   3. Slack / Google Chat へ通知(Webhook URL を設定した場合のみ)
 *   4. 毎朝、未対応の件数と期限切れの一覧をメールで送信
 *
 * 動作はすべて「設定」シートで変更できます(コードの編集は不要です)。
 */

const SHEET = { SETTINGS: '設定', ROUTING: '担当振り分け', LEDGER: '管理' };
const LEDGER_HEADER = [
  '受付番号', '受付日時', '氏名', 'メール', '種別',
  '内容', '担当者', 'ステータス', '対応期限', '備考',
];

// ---------------------------------------------------------------------------
// トリガーから呼ばれる関数
// ---------------------------------------------------------------------------

/** フォーム送信時(インストール型トリガー「フォーム送信時」に設定) */
function onFormSubmit(e) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const settings = readSettings_(ss);
  const answer = normalizeAnswer_(e.namedValues, settings);

  const record = buildRecord_(answer, settings, readRouting_(ss), new Date(), nextSerial_(ss));
  ledgerSheet_(ss).appendRow(LEDGER_HEADER.map((h) => record[h]));

  if (settings['自動返信'] === 'ON' && record['メール']) {
    MailApp.sendEmail({
      to: record['メール'],
      subject: fillTemplate_(settings['返信件名'], record),
      body: fillTemplate_(settings['返信本文'], record),
      name: settings['送信者名'] || undefined,
    });
  }
  if (settings['通知WebhookURL']) {
    notify_(settings['通知WebhookURL'], fillTemplate_(settings['通知文'], record));
  }
}

/** 毎朝のサマリー(時間主導型トリガー「日付ベース」に設定) */
function sendDailySummary() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const settings = readSettings_(ss);
  if (!settings['サマリー送信先']) return;

  const summary = summarize_(readLedger_(ss), new Date());
  if (summary.open === 0) return; // 未対応がなければ送らない

  MailApp.sendEmail({
    to: settings['サマリー送信先'],
    subject: `【毎朝のサマリー】未対応 ${summary.open}件(期限切れ ${summary.overdue.length}件)`,
    body: formatSummary_(summary),
  });
}

/** 初回のみ手動で実行:必要なシートとトリガーを作成 */
function setup() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  ensureSheet_(ss, SHEET.SETTINGS, [
    ['項目', '値'],
    ['自動返信', 'ON'],
    ['送信者名', '受付窓口'],
    ['返信件名', '【受付番号 {受付番号}】お問い合わせを受け付けました'],
    ['返信本文', '{氏名} 様\n\n' +
      'お問い合わせありがとうございます。\n' +
      '担当の {担当者} より、{対応期限} までにご連絡いたします。\n\n' +
      '受付番号:{受付番号}\n種別:{種別}'],
    ['通知WebhookURL', ''],
    ['通知文', '新しい問い合わせ #{受付番号}({種別}/担当:{担当者})\n{内容}'],
    ['対応日数', '3'],
    ['既定の担当者', '未割当'],
    ['サマリー送信先', ''],
    ['列名:氏名', 'お名前'],
    ['列名:メール', 'メールアドレス'],
    ['列名:種別', 'お問い合わせの種類'],
    ['列名:内容', 'お問い合わせ内容'],
  ]);
  ensureSheet_(ss, SHEET.ROUTING, [
    ['種別', '担当者'],
    ['見積もり', '営業 佐藤'],
    ['不具合', 'サポート 鈴木'],
  ]);
  ensureSheet_(ss, SHEET.LEDGER, [LEDGER_HEADER]);

  ScriptApp.getProjectTriggers().forEach((t) => ScriptApp.deleteTrigger(t));
  ScriptApp.newTrigger('onFormSubmit').forSpreadsheet(ss).onFormSubmit().create();
  ScriptApp.newTrigger('sendDailySummary').timeBased().everyDays(1).atHour(8).create();
}

// ---------------------------------------------------------------------------
// ロジック(副作用なし。test/run_test.js で単体テストしています)
// ---------------------------------------------------------------------------

/** フォームの列名の違いを吸収し、決まったキーに揃える */
function normalizeAnswer_(namedValues, settings) {
  const pick = (key) => {
    const col = settings['列名:' + key] || key;
    const v = namedValues[col];
    return Array.isArray(v) ? String(v[0] || '').trim() : String(v || '').trim();
  };
  return { 氏名: pick('氏名'), メール: pick('メール'), 種別: pick('種別'), 内容: pick('内容') };
}

/** 管理シートに書き込む1行分のデータを作る */
function buildRecord_(answer, settings, routing, now, serial) {
  const days = Number(settings['対応日数']) || 3;
  return {
    受付番号: formatSerial_(now, serial),
    受付日時: now,
    氏名: answer.氏名,
    メール: answer.メール,
    種別: answer.種別,
    内容: answer.内容,
    担当者: routing[answer.種別] || settings['既定の担当者'] || '未割当',
    ステータス: '未対応',
    対応期限: formatDate_(addBusinessDays_(now, days)),
    備考: '',
  };
}

/** 土日を除いて n 営業日後の日付を返す */
function addBusinessDays_(date, n) {
  const d = new Date(date.getTime());
  let left = n;
  while (left > 0) {
    d.setDate(d.getDate() + 1);
    const w = d.getDay();
    if (w !== 0 && w !== 6) left--;
  }
  return d;
}

/** 管理シートの行から、未対応件数と期限切れ一覧を集計する */
function summarize_(rows, today) {
  const todayStr = formatDate_(today);
  const open = rows.filter((r) => r['ステータス'] !== '完了');
  const overdue = open.filter((r) => r['対応期限'] && String(r['対応期限']) < todayStr);
  const byAssignee = {};
  open.forEach((r) => { byAssignee[r['担当者']] = (byAssignee[r['担当者']] || 0) + 1; });
  return { open: open.length, overdue, byAssignee };
}

function formatSummary_(s) {
  const lines = ['■ 担当者別の未対応件数'];
  Object.keys(s.byAssignee).forEach((k) => lines.push(`・${k}:${s.byAssignee[k]}件`));
  lines.push('', '■ 期限切れ');
  if (s.overdue.length === 0) lines.push('・なし');
  s.overdue.forEach((r) => {
    lines.push(`・#${r['受付番号']} ${r['種別']}` +
      `(担当:${r['担当者']}/期限:${r['対応期限']})`);
  });
  return lines.join('\n');
}

/** {キー} を record の値で置き換える */
function fillTemplate_(template, record) {
  return String(template || '').replace(/\{([^}]+)\}/g, (m, key) => {
    const v = record[key];
    if (v instanceof Date) return formatDate_(v);
    return v === undefined ? m : String(v);
  });
}

function formatSerial_(now, serial) {
  return formatDate_(now).replace(/-/g, '') + '-' + String(serial).padStart(3, '0');
}

function formatDate_(d) {
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

// ---------------------------------------------------------------------------
// スプレッドシート・外部サービスとのやりとり
// ---------------------------------------------------------------------------

function readSettings_(ss) {
  const values = ss.getSheetByName(SHEET.SETTINGS).getDataRange().getValues();
  const settings = {};
  values.slice(1).forEach(([k, v]) => { if (k) settings[String(k).trim()] = String(v); });
  return settings;
}

function readRouting_(ss) {
  const sheet = ss.getSheetByName(SHEET.ROUTING);
  const map = {};
  if (!sheet) return map;
  sheet.getDataRange().getValues().slice(1).forEach(([k, v]) => {
    if (k) map[String(k).trim()] = String(v).trim();
  });
  return map;
}

function readLedger_(ss) {
  const [header, ...rows] = ledgerSheet_(ss).getDataRange().getValues();
  return rows.map((r) => {
    const o = {};
    header.forEach((h, i) => { o[h] = r[i] instanceof Date ? formatDate_(r[i]) : r[i]; });
    return o;
  });
}

function ledgerSheet_(ss) {
  return ss.getSheetByName(SHEET.LEDGER) || ensureSheet_(ss, SHEET.LEDGER, [LEDGER_HEADER]);
}

/** 同じ日の受付番号の連番を決める(同時送信の重複を防ぐためロックを取る) */
function nextSerial_(ss) {
  const lock = LockService.getScriptLock();
  lock.waitLock(10000);
  try {
    const props = PropertiesService.getScriptProperties();
    const today = formatDate_(new Date());
    const sameDay = props.getProperty('serialDate') === today;
    const last = sameDay ? Number(props.getProperty('serial')) : 0;
    props.setProperties({ serialDate: today, serial: String(last + 1) });
    return last + 1;
  } finally {
    lock.releaseLock();
  }
}

/** Slack・Google Chat の Incoming Webhook({text} 形式)に送る */
function notify_(url, text) {
  try {
    UrlFetchApp.fetch(url, {
      method: 'post',
      contentType: 'application/json',
      payload: JSON.stringify({ text }),
      muteHttpExceptions: true,
    });
  } catch (err) {
    console.error('通知に失敗しました: ' + err); // 通知の失敗で転記・返信を止めない
  }
}

function ensureSheet_(ss, name, rows) {
  let sheet = ss.getSheetByName(name);
  if (!sheet) {
    sheet = ss.insertSheet(name);
    sheet.getRange(1, 1, rows.length, rows[0].length).setValues(rows);
    sheet.setFrozenRows(1);
  }
  return sheet;
}

if (typeof module !== 'undefined') {
  module.exports = {
    normalizeAnswer_, buildRecord_, addBusinessDays_, summarize_,
    formatSummary_, fillTemplate_, formatSerial_, formatDate_,
  };
}
