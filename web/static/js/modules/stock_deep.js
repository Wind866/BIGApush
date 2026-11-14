/**
 * 个股深度分析页面模块
 * 依赖后端 /api/stock/deep-analysis 接口
 * 提供：策略信号 + 买入建议 + 资金流向 + 基本面/业绩 的可交互查询与可视化
 */

let _deepInitialized = false;

export function initStockDeepPage() {
    const form = document.getElementById('deep-form');
    if (form && !_deepInitialized) {
        form.addEventListener('submit', onDeepSubmit);
        _deepInitialized = true;
    }
    const result = document.getElementById('stock-deep-result');
    if (result && !result.dataset.rendered) {
        result.innerHTML = '<div class="card"><div class="card-body" style="color:#6b7280;">' +
            '输入任意 A 股 6 位代码，选择分析周期后点击「深度分析」，即可查看策略信号、买入建议、资金流向与基本面/业绩。</div></div>';
    }
}

async function onDeepSubmit(e) {
    e.preventDefault();
    const codeEl = document.getElementById('deep-stock-code');
    const periodEl = document.getElementById('deep-period');
    const code = (codeEl.value || '').trim();
    const period = periodEl.value || '90d';
    if (!/^\d{6}$/.test(code)) {
        showDeepError('请输入 6 位数字股票代码');
        return;
    }
    setDeepLoading(true);
    hideDeepError();
    try {
        const resp = await fetch('/api/stock/deep-analysis', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ stock_code: code, period })
        });
        const json = await resp.json();
        setDeepLoading(false);
        if (!json.success) {
            showDeepError(json.message || '分析失败');
            return;
        }
        renderDeepResult(json.data || {});
    } catch (err) {
        setDeepLoading(false);
        showDeepError('请求失败：' + err.message);
    }
}

/* ============ 渲染 ============ */

function renderDeepResult(d) {
    const root = document.getElementById('stock-deep-result');
    if (!root) return;
    const si = d.stock_info || {};
    const code = si.code || si.stock_code || '';
    let html = '';
    html += renderHeader(si, code);
    html += renderBuyAdvice(d.buy_advice);
    html += renderStrategy(d.strategy_analysis);
    html += renderFundamental(d.fundamental);
    html += renderFundFlow(d.fund_flow);
    html += renderTechnical(d.technical);
    html += renderSector(d.sector);
    html += renderConclusion(d.conclusion);
    root.innerHTML = html;
    root.style.display = 'block';
    root.dataset.rendered = '1';
}

function renderHeader(si, code) {
    const name = si.name || si.stock_name || '—';
    return '<div class="card"><div class="card-body" style="display:flex;align-items:baseline;gap:12px;">' +
        `<h2 style="margin:0;">${esc(name)}</h2>` +
        `<span style="color:#6b7280;">${esc(code)}</span>` +
        '</div></div>';
}

function renderBuyAdvice(b) {
    if (!b) return '';
    const score = b.score != null ? b.score : 0;
    let html = '<div class="card"><div class="card-body">';
    html += '<h3 style="margin:0 0 12px;">综合买入建议</h3>';
    html += '<div style="display:flex;align-items:center;gap:16px;flex-wrap:wrap;">';
    html += actBadge(b.action);
    html += '<div style="flex:1;min-width:220px;">';
    html += `<div style="font-size:13px;color:#6b7280;margin-bottom:4px;">综合评分 ${score}/100（原评级：${esc(b.rating || '—')}）</div>`;
    html += `<div style="height:10px;background:#e5e7eb;border-radius:5px;overflow:hidden;">` +
        `<div style="height:100%;width:${score}%;background:${scoreColor(score)};"></div></div>`;
    html += '</div></div>';
    html += `<div style="margin-top:10px;font-size:13px;color:#374151;">看多信号 ${b.bull_count || 0} 个 ｜ 看空信号 ${b.bear_count || 0} 个</div>`;
    if (b.rationale && b.rationale.length) {
        html += '<div style="margin-top:10px;"><b>研判理由</b><ul style="margin:6px 0 0 18px;">';
        b.rationale.forEach(r => { html += `<li style="margin:3px 0;">${esc(r)}</li>`; });
        html += '</ul></div>';
    }
    if (b.risks && b.risks.length) {
        html += '<div style="margin-top:10px;color:#b91c1c;"><b>风险提示</b><ul style="margin:6px 0 0 18px;">';
        b.risks.forEach(r => { html += `<li style="margin:3px 0;">${esc(r)}</li>`; });
        html += '</ul></div>';
    }
    if (b.disclaimer) html += `<div style="margin-top:8px;font-size:12px;color:#9ca3af;">${esc(b.disclaimer)}</div>`;
    html += '</div></div>';
    return html;
}

function renderStrategy(list) {
    if (!list || !list.length) return '';
    let html = '<div class="card"><div class="card-body"><h3 style="margin:0 0 12px;">策略信号（多维度）</h3>' +
        '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:10px;">';
    list.forEach(s => {
        html += '<div style="border:1px solid #e5e7eb;border-radius:8px;padding:10px;">' +
            '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">' +
            `<span style="font-weight:600;">${esc(s.name)}</span>${sigBadge(s.signal)}</div>` +
            `<div style="font-size:12px;color:#6b7280;">${esc(s.note || '')}</div></div>`;
    });
    html += '</div></div></div>';
    return html;
}

function renderFundamental(f) {
    if (!f) return '';
    const val = f.valuation || {}, fin = f.financial || {}, gr = f.growth || {};
    let html = '<div class="card"><div class="card-body"><h3 style="margin:0 0 12px;">基本面与业绩</h3>';
    html += '<div style="margin-bottom:14px;"><b>估值</b><div style="display:flex;gap:18px;flex-wrap:wrap;margin-top:6px;font-size:14px;">';
    html += kv('市盈率(PE)', val.pe != null ? fmtNum(val.pe) : '—', val.pe_level);
    html += kv('市净率(PB)', val.pb != null ? fmtNum(val.pb) : '—', val.pb_level);
    html += kv('市销率(PS)', val.ps != null ? fmtNum(val.ps) : '—', val.ps_level);
    html += kv('总市值', val.market_cap != null ? fmtMoney(val.market_cap) : '—');
    html += '</div></div>';
    html += '<div style="margin-bottom:14px;"><b>财务质量</b><div style="display:flex;gap:18px;flex-wrap:wrap;margin-top:6px;font-size:14px;">';
    html += kv('营业收入', fin.revenue != null ? fmtMoney(fin.revenue) : '—');
    html += kv('净利润', fin.profit != null ? fmtMoney(fin.profit) : '—');
    html += kv('ROE', fin.roe != null ? fmtNum(fin.roe) + '%' : '—');
    html += kv('资产负债率', fin.debt_ratio != null ? fmtNum(fin.debt_ratio) + '%' : '—', fin.debt_level);
    html += '</div></div>';
    html += '<div><b>成长</b><div style="display:flex;gap:18px;flex-wrap:wrap;margin-top:6px;font-size:14px;">';
    html += kv('营收同比', gr.revenue_growth != null ? fmtPct(gr.revenue_growth) : '—');
    html += kv('净利润同比', gr.profit_growth != null ? fmtPct(gr.profit_growth) : '—', gr.growth_level);
    html += '</div></div>';
    html += '</div></div>';
    return html;
}

function renderFundFlow(ff) {
    if (!ff) return '';
    const flow = ff.flow_analysis || {};
    let html = '<div class="card"><div class="card-body"><h3 style="margin:0 0 12px;">资金流向</h3>' +
        '<div style="display:flex;gap:18px;flex-wrap:wrap;font-size:14px;">';
    html += kv('主力方向', flow.direction || '—');
    html += kv('主力净流入', flow.main_inflow != null ? fmtMoney(flow.main_inflow) : '—');
    html += kv('强度', flow.strength || '—');
    html += '</div>';
    const mf = ff.main_fund_analysis || {};
    const mfKeys = Object.keys(mf);
    if (mfKeys.length) {
        html += '<div style="margin-top:8px;font-size:12px;color:#6b7280;">主力资金细分：' +
            mfKeys.map(k => `${esc(k)} ${esc(mf[k])}`).join('，') + '</div>';
    }
    html += '</div></div>';
    return html;
}

function renderTechnical(t) {
    if (!t) return '';
    const ind = t.indicators || {};
    let html = '<div class="card"><div class="card-body"><h3 style="margin:0 0 12px;">技术面</h3>' +
        '<div style="display:flex;gap:18px;flex-wrap:wrap;font-size:14px;">';
    html += kv('趋势', t.trend || '—');
    ['MACD', 'KDJ', 'RSI', 'MA'].forEach(k => {
        if (ind[k] != null) html += kv(k, ind[k]);
    });
    html += '</div></div></div>';
    return html;
}

function renderSector(s) {
    if (!s) return '';
    let html = '<div class="card"><div class="card-body"><h3 style="margin:0 0 12px;">所属板块</h3><div style="font-size:14px;">';
    if (typeof s === 'object') {
        html += Object.entries(s).map(([k, v]) => `${esc(k)}：${esc(v)}`).join(' ｜ ');
    } else {
        html += esc(String(s));
    }
    html += '</div></div></div>';
    return html;
}

function renderConclusion(c) {
    if (!c) return '';
    let html = '<div class="card"><div class="card-body"><h3 style="margin:0 0 12px;">结论</h3>';
    html += `<div style="font-size:14px;margin-bottom:6px;"><b>评级：</b>${esc(c.rating || '—')}</div>`;
    if (c.reason) html += `<div style="font-size:14px;margin-bottom:6px;"><b>理由：</b>${esc(c.reason)}</div>`;
    if (c.risk) html += `<div style="font-size:14px;color:#b91c1c;"><b>风险：</b>${esc(c.risk)}</div>`;
    html += '</div></div>';
    return html;
}

/* ============ 工具 ============ */

function kv(k, v, tag) {
    let t = '';
    if (tag) t = ` <span style="font-size:11px;color:#6b7280;">(${esc(tag)})</span>`;
    return `<div><div style="color:#9ca3af;font-size:12px;">${k}</div><div>${v}${t}</div></div>`;
}

function sigBadge(signal) {
    const map = {
        '看多': ['#e53935', '#fdecea'],
        '看空': ['#2e7d32', '#e8f5e9'],
        '中性': ['#757575', '#f3f4f6']
    };
    const m = map[signal] || map['中性'];
    return `<span style="color:${m[0]};background:${m[1]};padding:2px 10px;border-radius:12px;font-size:12px;">${esc(signal)}</span>`;
}

function actBadge(action) {
    const map = {
        '强烈买入': ['#e53935', '#fdecea'],
        '买入': ['#e53935', '#fdecea'],
        '观望': ['#f59e0b', '#fef3c7'],
        '减仓': ['#2e7d32', '#e8f5e9'],
        '卖出': ['#2e7d32', '#e8f5e9']
    };
    const m = map[action] || ['#757575', '#f3f4f6'];
    return `<span style="color:${m[0]};background:${m[1]};font-size:20px;padding:8px 18px;border-radius:8px;font-weight:700;">${esc(action)}</span>`;
}

function scoreColor(s) {
    return s >= 60 ? '#e53935' : s >= 45 ? '#f59e0b' : '#2e7d32';
}

function fmtNum(v, digits = 2) {
    if (v === null || v === undefined || v === '' || (typeof v === 'number' && isNaN(v))) return '—';
    return Number(v).toLocaleString('zh-CN', { maximumFractionDigits: digits });
}

function fmtMoney(v) {
    if (v === null || v === undefined || isNaN(v)) return '—';
    const n = Number(v);
    if (Math.abs(n) >= 1e8) return (n / 1e8).toFixed(2) + '亿';
    if (Math.abs(n) >= 1e4) return (n / 1e4).toFixed(2) + '万';
    return n.toFixed(2);
}

function fmtPct(v) {
    if (v === null || v === undefined || isNaN(v)) return '—';
    return (Number(v) * 100).toFixed(1) + '%';
}

function esc(s) {
    if (s === null || s === undefined) return '';
    return String(s).replace(/[&<>"']/g, c =>
        ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function setDeepLoading(on) {
    const l = document.getElementById('deep-loading'); if (l) l.style.display = on ? 'inline' : 'none';
    const b = document.getElementById('deep-query-btn'); if (b) b.disabled = on;
    const ls = document.getElementById('deep-loading-state'); if (ls) ls.style.display = on ? 'block' : 'none';
    const r = document.getElementById('stock-deep-result');
    if (r) r.style.display = on ? 'none' : 'block';
}

function showDeepError(msg) {
    const e = document.getElementById('deep-error-state');
    const m = document.getElementById('deep-error-msg');
    if (m) m.textContent = msg;
    if (e) e.style.display = 'block';
    const r = document.getElementById('stock-deep-result');
    if (r) r.style.display = 'none';
}

function hideDeepError() {
    const e = document.getElementById('deep-error-state');
    if (e) e.style.display = 'none';
}
