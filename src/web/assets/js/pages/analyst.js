/**
 * AI Analyst: conversational analytics over the governance datasets.
 * Natural-language questions are answered with validated, read-only BigQuery SQL.
 */
import { h, mount, append, copyText } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { t, getLocale } from '../core/i18n.js';
import { fmt, isNum, DASH } from '../core/format.js';
import { api } from '../core/api.js';
import { store, setModel } from '../core/state.js';
import {
  pageHeader, card, dataTable, badge, button, banner, segmented, selectInput, markdownLite, codeBlock, toast,
} from '../core/components.js';
import { errorText } from '../core/messages.js';
import { barChart, lineChart, donutChart, colorAt } from '../core/charts.js';

const conversation = { messages: [] };

const STARTERS = [
  'Which models consumed the most tokens in the last 30 days?',
  'Show daily Gemini Enterprise prompts for the last 14 days.',
  'Who are the top 10 users by agent calls this month?',
  'Which Google Cloud services cost the most this month?',
  'Break down Model Armor blocks by filter type for the last 7 days.',
  'Which users uploaded the most files in the last 30 days?',
];

export default async function render(ctx) {
  const models = (store.boot.aiEngine && store.boot.aiEngine.models) || [];
  const header = pageHeader({
    title: t('AI Analyst'),
    desc: t('Ask questions in natural language. Answers are computed with validated, read-only BigQuery queries over your governance data.'),
    actions: button({ label: t('New conversation'), icon: 'plus', onClick: () => { conversation.messages = []; renderThread(); input.focus(); } }),
  });

  const thread = h('div', { class: 'chat-thread', 'aria-live': 'polite' });
  const input = h('textarea', { rows: 1, placeholder: t('Ask about usage, cost, agents, security or users'), 'aria-label': t('Question'), maxlength: 2000 });
  const sendBtn = button({ icon: 'send', variant: 'primary', title: t('Send'), size: 'sm' });
  const modelSelect = selectInput(models.map((m) => ({ value: m, label: m })), store.model || models[0], (v) => setModel(v), { 'aria-label': t('AI engine') });
  const composer = h('div', { class: 'composer' },
    h('div', { class: 'composer-box' }, input, sendBtn),
    h('div', { class: 'composer-meta' },
      h('span', null, t('Enter to send, Shift+Enter for a new line')),
      h('span', { class: 'row', title: t('The Gemini model that writes the query and the answer. Cost spike analysis uses the same model.') },
        icon('spark', { size: 13 }), t('Answer model'), modelSelect)));
  const main = h('div', { class: 'analyst-main' }, thread, composer);

  const side = h('div', { class: 'stack' },
    card({
      title: t('Suggested questions'),
      body: h('div', { class: 'suggestion-list' }, STARTERS.map((q) => h('button', { type: 'button', class: 'suggestion', onclick: () => ask(t(q)) }, t(q)))),
    }),
    card({
      title: t('How it works'),
      body: h('div', { class: 'stack-sm muted' },
        h('p', null, t('1. The selected Gemini model writes BigQuery SQL using the live schema of your audit, billing and connector tables.')),
        h('p', null, t('2. The query is validated: single read-only statement, allowlisted datasets only, byte-billing cap enforced.')),
        h('p', null, t('3. Results are summarized from the returned rows only. Inspect the SQL under every answer.'))),
    }));

  mount(ctx.root, header, h('div', { class: 'analyst' }, main, side));

  let busy = false;
  const autoSize = () => { input.style.setProperty('height', 'auto'); input.style.setProperty('height', `${Math.min(160, input.scrollHeight)}px`); };
  input.addEventListener('input', autoSize);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); ask(input.value); }
  });
  sendBtn.addEventListener('click', () => ask(input.value));

  function renderThread() {
    if (!conversation.messages.length) {
      mount(thread, h('div', { class: 'empty' },
        h('div', { class: 'empty-icon' }, icon('spark', { size: 18 })),
        h('div', { class: 'empty-title' }, t('Ask your governance data anything')),
        h('div', { class: 'empty-body' }, t('Usage by model, spend by service, Model Armor blocks, agent activity, licenses and more. Choose a suggestion or type a question.'))));
      return;
    }
    mount(thread, conversation.messages.map((m) => (m.role === 'user' ? userMsg(m) : assistantMsg(m, ask))));
    thread.scrollTop = thread.scrollHeight;
  }

  async function ask(question) {
    const q = String(question || '').trim();
    if (!q || busy) return;
    busy = true;
    sendBtn.disabled = true;
    input.value = '';
    autoSize();
    const history = conversation.messages.slice(-10).map((m) => ({ role: m.role === 'user' ? 'user' : 'model', text: (m.role === 'user' ? m.text : (m.result && m.result.answer) || '').slice(0, 4000) }));
    conversation.messages.push({ role: 'user', text: q });
    const pending = { role: 'assistant', pending: true };
    conversation.messages.push(pending);
    renderThread();
    try {
      const res = await api.post('/api/v1/analyst/chat', { question: q, history, model: store.model || undefined, locale: getLocale() });
      pending.pending = false;
      pending.result = res.data;
    } catch (err) {
      pending.pending = false;
      pending.error = err;
    } finally {
      busy = false;
      sendBtn.disabled = false;
      renderThread();
      input.focus();
    }
  }

  renderThread();
  setTimeout(() => input.focus(), 50);
  return null;
}

function userMsg(m) {
  return h('div', { class: ['msg', 'msg-user'] }, h('div', { class: 'bubble' }, m.text));
}

function assistantMsg(m, ask) {
  const mark = h('div', { class: 'assistant-mark' }, icon('spark', { size: 15 }));
  const bodyEl = h('div', { class: 'assistant-body' });
  if (m.pending) {
    append(bodyEl, [h('div', { class: 'typing', 'aria-label': t('Analyzing') }, h('span'), h('span'), h('span'))]);
  } else if (m.error) {
    append(bodyEl, [banner({ tone: 'danger', title: t('The question could not be answered'), text: errorText(m.error) })]);
  } else {
    const r = m.result || {};
    append(bodyEl, [markdownLite(r.answer || '')]);
    if (r.columns && r.columns.length && r.rows && r.rows.length) append(bodyEl, [resultCard(r)]);
    if (r.sql) {
      const details = h('details', null,
        h('summary', { class: 'muted' }, t('View SQL')),
        h('div', { class: 'stack-sm' }, codeBlock(r.sql)));
      details.style.setProperty('cursor', 'pointer');
      append(bodyEl, [details]);
    }
    append(bodyEl, [h('div', { class: 'msg-meta' },
      r.model ? h('span', null, r.model) : null,
      isNum(r.rowCount) ? h('span', null, t('{n} rows', { n: fmt.int(r.rowCount) })) : null,
      isNum(r.bytesProcessed) ? h('span', null, t('{b} processed', { b: fmt.bytes(r.bytesProcessed) })) : null,
      isNum(r.durationMs) ? h('span', null, fmt.duration(r.durationMs)) : null,
      isNum(r.attempts) && r.attempts > 1 ? h('span', null, t('Self-corrected after {n} attempts', { n: r.attempts })) : null)]);
    if (r.suggestions && r.suggestions.length) {
      append(bodyEl, [h('div', { class: 'chips' }, r.suggestions.slice(0, 3).map((s) => h('button', { type: 'button', class: 'chip', onclick: () => ask(s) }, s)))]);
    }
  }
  return h('div', { class: ['msg', 'msg-assistant'] }, mark, bodyEl);
}

function toNumber(v) {
  if (isNum(v)) return v;
  if (typeof v === 'string' && v.trim() !== '' && !Number.isNaN(Number(v))) return Number(v);
  return null;
}

function resultCard(r) {
  const cols = r.columns.map((c) => c.name);
  const rows = r.rows.map((row) => Object.fromEntries(cols.map((c, i) => [c, row[i]])));
  let chartType = r.chart && r.chart.type;
  const labelCol = (r.chart && r.chart.labelColumn) || cols[0];
  let valueCols = (r.chart && r.chart.valueColumns) || [];
  if (!valueCols.length) valueCols = cols.filter((c) => c !== labelCol && rows.every((row) => row[c] === null || toNumber(row[c]) !== null)).slice(0, 3);
  const chartable = valueCols.length > 0 && rows.length > 1 && rows.length <= 120;
  if (!chartType && chartable) chartType = /^\d{4}-\d{2}-\d{2}/.test(String(rows[0][labelCol])) ? 'line' : (rows.length <= 6 ? 'doughnut' : 'bar');
  const chartHost = h('div');
  const draw = (type) => {
    const buckets = rows.map((row) => String(row[labelCol] === null ? DASH : row[labelCol]).slice(0, 40));
    if (type === 'doughnut') {
      donutChart(chartHost, { items: rows.slice(0, 10).map((row, i) => ({ label: String(row[labelCol]), value: toNumber(row[valueCols[0]]) || 0, color: colorAt(i) })), format: fmt.compact, size: 150 });
      return;
    }
    const series = valueCols.map((c, i) => ({ key: c, name: c, values: rows.map((row) => toNumber(row[c]) || 0), color: colorAt(i) }));
    const opts = { buckets, series, height: 240, valueFormat: (v) => fmt.num(v, 2), yFormat: fmt.compact };
    if (type === 'line') lineChart(chartHost, opts); else barChart(chartHost, opts);
  };
  const head = h('div', { class: 'rc-head' },
    h('span', null, t('{n} rows', { n: fmt.int(r.rowCount || rows.length) }), r.truncated ? ` \u00b7 ${t('truncated')}` : ''),
    chartable ? segmented([{ value: 'bar', label: t('Bar') }, { value: 'line', label: t('Line') }, { value: 'doughnut', label: t('Donut') }], chartType || 'bar', (v) => draw(v)) : null);
  const table = dataTable({
    columns: cols.map((c) => ({
      key: c, label: c,
      align: rows.every((row) => row[c] === null || toNumber(row[c]) !== null) ? 'num' : '',
      render: (row) => {
        const v = row[c];
        if (v === null || v === undefined) return DASH;
        const n = toNumber(v);
        return n !== null && typeof v !== 'boolean' ? fmt.num(n, 2) : String(v);
      },
      value: (row) => { const n = toNumber(row[c]); return n !== null ? n : row[c]; },
    })),
    rows, pageSize: 8, searchable: rows.length > 8, exportName: 'analyst-result',
  });
  const cardEl = h('div', { class: 'result-card' }, head, chartable ? h('div', { class: 'rc-body' }, chartHost) : null, table);
  if (chartable) requestAnimationFrame(() => draw(chartType || 'bar'));
  return cardEl;
}
