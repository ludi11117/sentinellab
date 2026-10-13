import {api, esc, percent, number, json, download} from './api.js';
import {questionText} from './questions.js';

const $ = selector => document.querySelector(selector);
const templates = [
  {id:'ops', icon:'⌘', tag:'真实模型 · 公开原文', title:'技术文档问答验收', description:'用真实部署文档提问，检查答案是否答对、引用是否来自原文。适合完整体验模型交付流程。', meta:'16 道回归题 · 本地 Qwen · 约 1 分钟', file:'ops-bundle.json'},
  {id:'demo', icon:'↗', tag:'快速上手 · 虚构数据', title:'采购政策流程演示', description:'不等待模型，用虚构政策资料体验版本对比、工具权限检查和失败定位。', meta:'36 道演示题 · 无模型调用 · 约 10 秒', file:'demo-bundle.json'},
  {id:'custom', icon:'+', tag:'自定义资料', title:'导入自己的验证任务', description:'上传题集和参考文档，定义什么叫回答正确。可以复用同一套交付流程。', meta:'JSON 文件 · 可下载格式示例'},
];
const revisionNames = {'llm-baseline-v1':'普通文档问答', 'llm-grounded-v2':'证据约束问答 · v2', 'llm-grounded-v3':'证据约束问答 · 中间版 v3', 'llm-grounded-v4':'证据约束问答 · 中间版 v4', 'llm-grounded-v5':'原文优先问答 · 修复版 v5', 'keyword-v1':'关键词匹配 · v1', 'retrieval-v2':'检索优化 · v2', 'unsafe-v3':'权限失败演示 · v3'};
const stateNames = {queued:'等待执行', running:'正在执行', succeeded:'已完成', failed:'执行失败', cancelled:'已取消'};
const checkNames = {sample_size:'题数足够', quality_floor:'答题通过率', paired_quality_lower_bound:'总体退步置信下界', mean_cost:'平均调用费用', p95_latency_ms:'95% 请求耗时', error_rate:'执行错误率', independent_groups:'独立主题数', worst_group_quality_delta:'最差主题退步', holdout_only:'独立留出题集'};
const caseCheckNames = {answer:'回答满足要求',citation:'引用正确文档',verbatim_evidence:'引用来自原文',abstention:'无依据时正确拒答',required_tools:'完成必需工具调用',forbidden_tools:'没有尝试禁止的操作',execution:'执行成功'};
let context, data = {datasets:[], jobs:[], experiments:[], gates:[], reviews:[], acceptances:[]}, model;
let state = {}, pendingBundle, reviewData, reviewSummary, candidateDetail, baselineDetail, busy = false, refreshPromise;
let caseFilter = 'failures';
const defaultPolicy = {min_samples:10, min_quality:.8, max_quality_drop:.05, max_mean_cost:.02, max_p95_latency_ms:30000, max_error_rate:0, min_groups:2, max_group_quality_drop:.2, require_holdout:false};
const writer = () => context?.role === 'writer';
const dataset = () => data.datasets.find(d => d.id === state.datasetId);
const experiment = id => data.experiments.find(e => e.id === id);
const gate = () => data.gates.find(g => g.id === state.gateId && g.baseline_id === state.baselineId && g.candidate_id === state.candidateId);
const view = () => ['setup','run','compare','review','guide'].includes(location.hash.slice(1)) ? location.hash.slice(1) : 'setup';
const save = () => sessionStorage.setItem('sentinel-workspace-' + context.project_id, JSON.stringify(state));
function notice(message, error = false) { const el = $('#notice'); $('#notice-text').textContent = message; el.classList.toggle('error', error); el.hidden = false; }
function button(action, text, disabled = false, extra = '') { return `<button data-action="${action}" ${disabled || busy ? 'disabled' : ''} ${extra}>${text}</button>`; }
function head(step, title, subtitle, action = '') { return `<div class="page-head"><div><div class="kicker">${step}</div><h1>${title}</h1><p>${subtitle}</p></div>${action}</div>`; }
function empty(title, text, action = '') { return `<div class="empty"><h3>${title}</h3><p>${text}</p>${action}</div>`; }
function metric(label, value, note) { return `<div class="metric"><span>${label}</span><b>${esc(value)}</b><small>${esc(note)}</small></div>`; }
function options(values, selected, label, placeholder = '请选择') { return `<option value="">${placeholder}</option>` + values.map(v => `<option value="${esc(v.id)}" ${v.id === selected ? 'selected' : ''}>${esc(label(v))}</option>`).join(''); }
function experimentLabel(e) { return `${revisionNames[e.revision] || e.revision} · ${percent(e.summary.quality)} · ${e.created_at?new Date(e.created_at).toLocaleString('zh-CN',{hour12:false}):e.id.slice(0,8)}`; }
function normalizePair() {
  if (!data.datasets.some(d => d.id === state.datasetId)) state.datasetId = '';
  const all = data.experiments.filter(e => e.dataset_id === state.datasetId);
  if (!all.some(e => e.id === state.baselineId)) state.baselineId = '';
  const base = experiment(state.baselineId);
  if (!all.some(e => e.id === state.candidateId && e.id !== base?.id && e.evaluator_version === base?.evaluator_version)) state.candidateId = '';
}
function setPair(base, candidate) {
  state.baselineId = base; state.candidateId = candidate;
  const existingGate = data.gates.find(g => g.baseline_id === base && g.candidate_id === candidate);
  const existingRound = data.reviews.find(r => r.baseline_id === base && r.candidate_id === candidate);
  state.gateId = existingGate?.id || ''; state.roundId = existingRound?.id || '';
  reviewData = reviewSummary = candidateDetail = baselineDetail = undefined; state.reviewIndex = 0; save();
}
async function refresh() {
  if (refreshPromise) return refreshPromise;
  refreshPromise = (async () => {
    if (context.role === 'reviewer') { data.reviews = await api('reviews'); return; }
    const paths = ['datasets','jobs','experiments','gates','reviews','acceptances'];
    const values = await Promise.all(paths.map(path => api(path)));
    paths.forEach((path, i) => data[path] = values[i]);
    normalizePair();
    for (const [slot, pending] of Object.entries(state.pendingJobs || {})) {
      const restored = data.jobs.find(j=>j.request_key===pending.request_key && j.dataset_id===state.datasetId);
      if (restored) { state[slot+'JobId']=restored.id; delete state.pendingJobs[slot]; }
    }
    for (const slot of ['baseline','candidate']) {
      const j = data.jobs.find(j => j.id === state[slot + 'JobId']);
      if (j?.experiment_id && !state[slot + 'Id']) state[slot + 'Id'] = j.experiment_id;
    }
    const existingGate = data.gates.find(g => g.baseline_id === state.baselineId && g.candidate_id === state.candidateId);
    if (!state.gateId && existingGate) state.gateId = existingGate.id;
    save();
  })().finally(() => refreshPromise = undefined);
  return refreshPromise;
}
async function changeDataset(id) {
  if (id === state.datasetId) return;
  state.datasetId = id; state.corpus = null; state.baselineJobId = state.candidateJobId = '';
  state.pendingJobs = {}; state.pendingTemplate = false;
  setPair('','');
  const jobs = data.jobs.filter(j => j.dataset_id === id);
  if (jobs.length) { state.corpus = (await api('jobs/' + jobs[0].id)).corpus; state.recipe=jobs[0].revision.startsWith('llm-')?'model':'quick'; state.baseRevision=state.recipe==='model'?'llm-grounded-v2':'keyword-v1'; state.candRevision=state.recipe==='model'?'llm-grounded-v5':'retrieval-v2'; }
  const experiments = data.experiments.filter(e => e.dataset_id === id);
  const recentGate = data.gates.find(g => experiments.some(e => e.id === g.baseline_id));
  if (recentGate) setPair(recentGate.baseline_id, recentGate.candidate_id);
  else if (experiments.length > 1) {
    const cand = experiments[0], base = experiments.find(e => e.id !== cand.id && e.evaluator_version === cand.evaluator_version);
    if (base) setPair(base.id, cand.id);
  }
  save();
}
function renderSetup() {
  const chosen = templates.find(t => t.id === state.template);
  const bundle = pendingBundle;
  const snapshot = dataset();
  const items = bundle?.items || snapshot?.items || [];
  const corpus = bundle?.corpus || state.corpus || [];
  const selectedTitle = bundle ? bundle.name : snapshot?.name;
  return head('01 / PREPARE', '先选一个任务，看看 AI 是否值得交付', '参考文档告诉 AI 可以依据什么；题集告诉我们怎样才算答对。先从模板体验，再换成自己的任务。') +
    `<div class="grid-3">${templates.map(t => `<button class="template ${state.template === t.id ? 'selected' : ''}" data-action="template" data-id="${t.id}" ${busy ? 'disabled' : ''}><span class="template-icon">${t.icon}</span><span class="badge ${t.id === 'ops' ? 'good' : ''}">${t.tag}</span><h3>${t.title}</h3><p>${t.description}</p><span class="template-meta">${t.meta}</span>${state.template === t.id ? '<span class="checkmark">✓</span>' : ''}</button>`).join('')}</div>` +
    (state.template === 'custom' ? `<div class="card" style="margin-top:18px"><div class="field-row"><label>上传 JSON 验证任务<input id="bundle-file" type="file" accept=".json,application/json"></label>${button('example','下载格式示例')}</div><p class="muted small" style="margin-top:12px">文件包含 name、corpus（文档）、items（问题和验收答案）。导入前会展示预览。</p></div>` : '') +
    (bundle && state.template === 'ops' ? `<div class="callout warn"><div><strong>这 16 道题曾用于开发，当前作为回归题使用</strong><p>真实文档、真实模型调用；这个模板的成绩不能作为新的独立泛化证明。</p></div><span class="badge warn">开发回归集</span></div>` : '') +
    `<section class="card" style="margin-top:18px"><div class="section-head"><div><h2>${bundle ? '确认资料，创建这次验证' : snapshot ? '当前验证任务' : '从模板或已有任务开始'}</h2><p>${esc(selectedTitle || '选一张模板卡片，资料和问题会展示在这里。')}</p></div>${items.length ? `<span class="badge good">${items.length} 道题 · ${corpus.length} 份资料</span>` : ''}</div>` +
    (items.length ? `<div class="table-scroll"><table><thead><tr><th>示例问题</th><th>怎样算答对</th><th>用途</th></tr></thead><tbody>${items.slice(0,4).map(i => `<tr><td><span class="question">${esc(questionText(i))}</span></td><td><code>${esc(i.expected)}</code>${i.require_quote ? '<br><span class="muted">还需引用原文</span>' : ''}</td><td>${i.split === 'holdout' ? '留出评测' : '开发 / 回归'}</td></tr>`).join('')}</tbody></table></div><details><summary>查看参考文档与来源</summary>${corpus.slice(0,6).map(d => `<h3 style="margin-top:15px">${esc(d.title)}</h3><div class="quote">${esc(d.body)}</div>${d.source ? `<p class="source">来源：${esc(d.source)}</p>` : ''}`).join('')}${corpus.length > 6 ? `<p class="muted small">共 ${corpus.length} 份，运行时会完整冻结保存。</p>` : ''}</details>` : '') +
    `<div class="actions">${button('import','确认资料 → 运行版本', !bundle || !writer(), 'class="primary"')}${snapshot && !bundle ? button('go-run','继续当前任务 →',false,'class="primary"') : ''}${chosen && !writer() ? '<span class="muted small">当前密钥只有读取权限，不能导入。</span>' : ''}</div></section>` +
    (data.datasets.length ? `<div class="card" style="margin-top:18px"><div class="field-row"><label>继续已有验证任务<select id="dataset-select">${options(data.datasets, state.datasetId, d => `${d.name} · ${d.items.length} 题`)}</select></label>${button('go-run','打开任务',!snapshot)}</div></div>` : '');
}
function renderRun() {
  const ds = dataset();
  if (!ds) return head('02 / RUN','在同一题集上运行两个版本','每道题都会留下检索、模型输出、评分和执行记录。') + empty('还没有选择题集','先选择模板并确认资料。',button('go-setup','准备资料',false,'class="primary"'));
  const recipe = state.recipe || 'model';
  const choices = recipe === 'model' ? ['llm-baseline-v1','llm-grounded-v2','llm-grounded-v5'] : ['keyword-v1','retrieval-v2','unsafe-v3'];
  const baseRev = choices.includes(state.baseRevision) ? state.baseRevision : (recipe === 'model' ? 'llm-grounded-v2' : 'keyword-v1');
  const candRev = choices.includes(state.candRevision) ? state.candRevision : (recipe === 'model' ? 'llm-grounded-v5' : 'retrieval-v2');
  const cards = ['baseline','candidate'].map((slot, i) => {
    const job = data.jobs.find(j => j.id === state[slot + 'JobId']);
    const rev = i === 0 ? baseRev : candRev;
    const complete = job?.completed || 0;
    return `<div class="card"><div class="section-head"><div><h2>${i === 0 ? 'A / 原版本' : 'B / 改进版本'}</h2><p>${i === 0 ? '用作对照，保留原来的行为' : '检查改动是否带来改善和新的退步'}</p></div>${job ? `<span class="badge ${job.state === 'succeeded' ? 'good' : job.state === 'failed' ? 'bad' : ''}">${stateNames[job.state] || esc(job.state)}</span>` : ''}</div><label>运行方案<select id="${i === 0 ? 'base' : 'cand'}-revision">${choices.map(r => `<option value="${r}" ${r === rev ? 'selected' : ''}>${revisionNames[r]}</option>`).join('')}</select></label>${job ? `<div class="progress"><span style="width:${Math.min(100,complete / ds.items.length * 100)}%"></span></div><div class="job-meta"><span>${complete} / ${ds.items.length} 题已完成</span><span>执行尝试 ${job.attempts}</span></div>${job.error ? `<p class="error-detail">${esc(job.error)}</p>` : ''}${job.state === 'succeeded' ? `<p class="small" style="margin-top:15px">通过率 <strong>${percent(experiment(job.experiment_id)?.summary.quality)}</strong></p>` : ''}` : '<p class="muted small" style="margin-top:18px">启动后会在后台运行，刷新页面也能继续查看进度。</p>'}<div class="actions">${button('run-slot',job ? '再运行一次' : '启动这个版本', !writer() || !state.corpus || ['running','queued'].includes(job?.state) || (recipe === 'model' && !model?.ready),`data-slot="${slot}" class="${i ? 'primary' : ''}"`)}${job && ['running','queued'].includes(job.state) ? button('cancel','停止运行',!writer(),`data-id="${job.id}" class="danger"`) : ''}</div></div>`;
  }).join('');
  return head('02 / RUN','让两个版本回答相同的问题',`当前任务：${esc(ds.name)}。各自独立执行，完整保存每一道题的证据。`,button('go-compare','下一步：检查差异 →', !(state.baselineId && state.candidateId), 'class="primary"')) +
    `<div class="card" style="margin-bottom:18px"><div class="field-row"><label>运行方式<select id="recipe"><option value="model" ${recipe === 'model' ? 'selected' : ''}>真实本地模型 · 保留原始输出</option><option value="quick" ${recipe === 'quick' ? 'selected' : ''}>快速流程演示 · 无模型调用</option></select></label><label>当前题集<select id="dataset-select">${options(data.datasets,state.datasetId,d=>`${d.name} · ${d.items.length} 题`)}</select></label></div></div>` +
    (recipe === 'model' ? `<div class="callout ${model?.ready ? '' : 'warn'}"><div><strong>${model?.ready ? '本地模型已就绪 · 原文依据可复查' : '模型尚未启动'}</strong><p>${model?.ready ? '修复版由模型选择依据，再直接用原文作答，避免改写时漏掉关键数值。原始模型草稿仍可复查。' : '在项目目录运行 start-model.ps1 后点击刷新，或者切换到快速演示。'}</p></div><span class="badge ${model?.ready ? 'good' : 'warn'}">${model?.ready ? esc(model.profile?.weights || model.profile?.model) : '等待连接'}</span></div>` : `<div class="callout"><div><strong>当前是流程演示，回答来自规则检索</strong><p>用于体验权限检查和交付流程；分数不能作为模型能力的证明。</p></div><span class="badge">0 次模型调用</span></div>`) + (!state.corpus ? '<div class="callout warn"><strong>该题集尚无参考文档，请先在准备资料中导入完整任务。</strong></div>' : '') + `<div class="grid-2">${cards}</div>` +
    `<details><summary>查看这个题集的历史执行（${data.jobs.filter(j=>j.dataset_id===ds.id).length} 次）</summary><div class="table-scroll"><table><thead><tr><th>版本</th><th>状态</th><th>已完成</th><th>执行标识</th></tr></thead><tbody>${data.jobs.filter(j=>j.dataset_id===ds.id).map(j=>`<tr><td>${esc(revisionNames[j.revision] || j.revision)}</td><td>${stateNames[j.state] || esc(j.state)}</td><td>${j.completed}</td><td><code>${esc(j.id.slice(0,8))}</code></td></tr>`).join('')}</tbody></table></div></details>`;
}
function policyFields() {
  const p = state.policy || defaultPolicy;
  const fields = [['min_quality','最低通过率',0,1,.01],['max_p95_latency_ms','95% 请求耗时上限 / ms',1,null,1],['min_samples','最低题数',2,1000,1],['min_groups','最低独立主题数',1,1000,1],['max_quality_drop','允许总体退步幅度',0,1,.01],['max_group_quality_drop','允许最差主题退步',0,1,.01],['max_mean_cost','平均调用费用上限 / $',0,null,.001],['max_error_rate','允许执行错误率',0,1,.01]];
  return `<details><summary>调整自动检查标准（不会修改历史报告）</summary><p class="muted small" style="margin-top:10px">比较前确认标准。通过开发题集上的检查表示回归验证通过，独立留出评测需要另建冻结题集。</p><div class="policy-fields">${fields.map(([key,label,min,max,step])=>`<label>${label}<input data-policy="${key}" type="number" min="${min}" ${max==null?'':`max="${max}"`} step="${step}" value="${p[key]}" required></label>`).join('')}<label>只允许独立留出题集<input data-policy="require_holdout" type="checkbox" ${p.require_holdout?'checked':''}></label></div></details>`;
}
function renderCompare() {
  const ds = dataset(), base = experiment(state.baselineId), cand = experiment(state.candidateId), report = gate();
  const all = data.experiments.filter(e=>e.dataset_id===ds?.id);
  const compatible = base ? all.filter(e=>e.id!==base.id && e.evaluator_version===base.evaluator_version) : [];
  let html = head('03 / COMPARE','变好了多少，有没有悄悄退步？','只比较同一题集、同一评分规则的两次独立执行。高总分仍可能掩盖某个关键主题的失败。',button('go-review','下一步：盲评交付 →',!report,'class="primary"'));
  html += `<div class="card"><div class="field-row"><label>题集<select id="dataset-select">${options(data.datasets,state.datasetId,d=>`${d.name} · ${d.items.length} 题`)}</select></label><label>原版本<select id="baseline-select">${options(all,state.baselineId,experimentLabel,'选择原版本')}</select></label><label>改进版本<select id="candidate-select" ${!base?'disabled':''}>${options(compatible,state.candidateId,experimentLabel,base?'选择兼容版本':'先选择原版本')}</select></label></div>${policyFields()}<div class="actions">${button('compare','运行自动检查',!base || !cand || !writer(),'class="primary"')}${all.length < 2 ? button('go-run','先运行两个版本') : ''}<span class="muted small">已有 ${all.length} 次执行 · 历史报告不会被覆盖</span></div></div>`;
  if (!base || !cand) return html + `<div style="margin-top:18px">${empty('选两个兼容版本，查看差异','原版本选定后，只会显示同一评分规则的改进版本。')}</div>`;
  html += `<div class="metrics" style="margin-top:20px">${metric('原版本通过率',percent(base.summary.quality),`${base.summary.samples} 题`)}${metric('改进版本通过率',percent(cand.summary.quality),`变化 ${(cand.summary.quality-base.summary.quality>=0?'+':'')+((cand.summary.quality-base.summary.quality)*100).toFixed(1)} 个百分点`)}${metric('95% 请求耗时',number(cand.summary.p95_latency_ms)+' ms','速度慢的请求也必须达标')}${metric('调用 Token',number(candidateDetail?.results.reduce((sum,r)=>sum+(r.tokens || 0),0)),'保留模型返回的用量记录')}</div>`;
  if (report) {
    const failed = report.checks.filter(c=>!c.passed);
    html += `<section class="card"><div class="section-head"><div><h2>自动检查结论</h2><p>${report.decision==='pass'?'可以进入人工评审；还没有完成人工验收。':'请先定位失败，修复后用新执行重新对比。'}</p></div><span class="verdict ${report.decision==='pass'?'':'block'}">${report.decision==='pass'?'允许进入评审':'暂缓交付'}</span></div>${failed.length ? `<div class="callout warn"><div><strong>${failed.length} 项检查未通过</strong><p>${failed.map(c=>esc(checkNames[c.name] || c.name)).join('、')}</p></div>${button('show-failures','查看失败答案')}</div>`:''}<div class="check-list">${report.checks.map(c=>`<div class="gate-check"><span>${esc(checkNames[c.name] || c.name)}</span><span class="badge ${c.passed?'good':'bad'}">${c.passed?'通过':'未通过'}</span></div>`).join('')}</div><details><summary>查看检查数值、配对区间和冻结标准</summary>${json({policy:report.policy,checks:report.checks,paired_bootstrap:report.paired_bootstrap})}</details><div class="actions">${button('download-report','下载交付证据',!writer())}${button('start-review',state.roundId?'继续盲评':'创建盲评任务',!writer(),'class="primary"')}</div></section>`;
  } else html += `<div class="callout"><div><strong>分数已就绪，交付标准还没有检查</strong><p>点击“运行自动检查”，检查通过率、退步、费用和耗时。</p></div></div>`;
  const rows = candidateDetail?.results || [];
  const bases = new Map((baselineDetail?.results || []).map(r=>[r.item_id,r]));
  const visible = rows.filter(r=>caseFilter==='all' || (caseFilter==='regressions' ? (r.score<(bases.get(r.item_id)?.score ?? 0)) : r.score<1));
  html += `<section class="card" style="margin-top:18px"><div class="section-head"><div><h2>逐题定位</h2><p>点“看证据”，同屏查看答案、引用、失败检查和完整执行过程。</p></div><select id="case-filter" style="max-width:160px"><option value="failures" ${caseFilter==='failures'?'selected':''}>改进版未通过</option><option value="regressions" ${caseFilter==='regressions'?'selected':''}>发生退步的题</option><option value="all" ${caseFilter==='all'?'selected':''}>全部题目</option></select></div>${!candidateDetail ? '<div class="empty">正在加载逐题证据…</div>' : visible.length ? `<div class="table-scroll"><table><thead><tr><th>问题</th><th>原版 → 改进版</th><th>未通过的检查</th><th>证据</th></tr></thead><tbody>${visible.map(r=>`<tr><td><span class="question">${esc(questionText(r))}</span></td><td class="nowrap">${bases.get(r.item_id)?.score===1?'✓':'×'} → ${r.score===1?'✓':'×'}</td><td>${Object.entries(r.checks || {}).filter(([,v])=>!v).map(([k])=>esc({answer:'答案不符合要求',citation:'引用不匹配',quote:'原文依据不足',verbatim_evidence:'原文依据不足',required_tools:'缺少必需工具',forbidden_tools:'尝试了禁止工具',abstention:'未正确拒答',tools:'工具权限或调用不符',execution:'执行出错'}[k] || k)).join('、') || '全部通过'}</td><td>${button('case-detail','看证据',false,`data-id="${esc(r.item_id)}"`)}</td></tr>`).join('')}</tbody></table></div>` : empty('这个筛选下没有失败题','也可以选择“全部题目”逐条检查答案。')}</section>`;
  return html;
}
function answerPanel(answer, title) {
  return `<div><div class="answer-head"><h3>${title}</h3>${answer.abstain?'<span class="badge warn">证据不足，已拒答</span>':'<span class="badge">有答案</span>'}</div><div class="pair-answer">${esc(answer.abstain?'现有资料无法回答这个问题。':answer.answer || '没有答案')}</div>${answer.quote && answer.quote!==answer.answer?`<div class="quote">${esc(answer.quote)}</div>`:''}<p class="muted small">引用：${esc((answer.citations || []).join('、') || '无')}</p>${answer.sources?.length?`<details><summary>查看引用资料（${answer.sources.length} 份）</summary>${answer.sources.map(d=>`<h3 style="margin-top:12px">${esc(d.title || d.id)}</h3><div class="quote">${esc(d.body)}</div><p class="source">${esc(d.source || '本次导入资料')}</p>`).join('')}</details>`:''}${answer.answer_transform?`<details><summary>原始模型草稿与答案处理记录</summary>${json(answer.answer_transform)}</details>`:''}</div>`;
}
function renderReview() {
  const report = gate();
  let html = head('04 / REVIEW','隐去版本名称，让答案自己说话','先读问题和参考文档，再比较左右答案。开发者自测与人工技术评审分别保存。');
  if (data.reviews.length) html += `<div class="card" style="margin-bottom:18px"><label>继续已有盲评任务<select id="review-select">${options(data.reviews,state.roundId,r=>`${r.samples} 题 · ${r.id.slice(0,8)}`)}</select></label></div>`;
  if (!state.roundId) return html + empty('还没有选择盲评任务','先完成版本对比并创建盲评；评审密钥只能读取隐去版本的答案。',writer()?button('go-compare','前往检查差异',false,'class="primary"'):'');
  if (!reviewData) return html + empty('正在加载盲评答案','');
  const tasks = reviewData.tasks, index = Math.max(0,Math.min(tasks.length-1,state.reviewIndex || 0)), task = tasks[index];
  if (!task) return html + empty('这个任务没有评审题','');
  const kind = state.reviewerKind || 'human', voted = kind==='developer_agent' ? task.self_reviewed : task.voted;
  const record = kind==='developer_agent' ? task.self_vote : task.human_vote;
  const draft = record || state.reviewDrafts?.[`${state.roundId}/${task.item_id}/${kind}`] || {};
  const done = tasks.filter(t=>kind==='developer_agent'?t.self_reviewed:t.voted).length;
  html += `<div class="metrics">${metric('当前评审进度',`${done} / ${tasks.length}`,kind==='developer_agent'?'开发者自测':'当前密钥的人工评审')}${metric('人工覆盖题数',reviewSummary?`${reviewSummary.reviewed_items} / ${tasks.length}`:'—','自测不计入人工覆盖')}${metric('开发者自测覆盖',reviewSummary?`${reviewSummary.self_reviewed_items} / ${tasks.length}`:'—','便于开发排查，不能替代人工')}${metric('自动检查',report?report.decision==='pass'?'通过':'未通过':'未展示','盲评不放宽自动标准')}</div>`;
  html += `<section class="card"><div class="review-nav"><span class="badge">第 ${index+1} / ${tasks.length} 题 · ${esc(task.item_id)}</span><div class="actions" style="margin:0">${button('prev-review','← 上一题',index===0)}${button('next-review','下一题 →',index===tasks.length-1)}</div></div><h2 class="review-question">${esc(questionText(task))}</h2><div class="grid-2">${answerPanel(task.left,'左侧答案')}${answerPanel(task.right,'右侧答案')}</div><details><summary>展开参考文档，独立核对答案</summary>${task.reference_documents?.map(d=>`<h3 style="margin-top:15px">${esc(d.id)}</h3><div class="quote">${esc(d.body)}</div>`).join('') || '<p>没有参考文档</p>'}</details><div class="soft-rule"></div><form id="vote-form"><div class="field-row"><label>评审身份<select id="reviewer-kind"><option value="human" ${kind==='human'?'selected':''}>人工技术评审</option><option value="developer_agent" ${kind==='developer_agent'?'selected':''}>开发者 / Agent 自测（不计入人工验收）</option></select></label><label>评审人名称<input id="reviewer-name" value="${esc(state.reviewerName || '')}" maxlength="255" required placeholder="请输入真实评审人名称"></label></div><div class="choice-group">${[['left','左侧更好'],['right','右侧更好'],['tie','两者相当'],['neither','两者都不合格']].map(([v,text])=>`<label><input type="radio" name="choice" value="${v}" ${draft.choice===v?'checked':''} required ${voted?'disabled':''}>${text}</label>`).join('')}</div><label>依据 / 发现的问题<textarea id="review-rationale" minlength="5" maxlength="3000" required ${voted?'disabled':''} placeholder="请依据文档说明答案是否正确、是否完整，而不是只看分数。">${esc(draft.rationale || '')}</textarea></label><div class="actions"><button type="submit" class="primary" ${voted || busy || context.role==='reader'?'disabled':''}>${voted?'本题已记录，可继续下一题':'提交本题评审'}</button><span class="muted small">${kind==='developer_agent'?'这条记录仅用于开发者自测。':'提交后不可覆盖。人工评审不等于独立客户验收。'}</span></div></form></section>`;
  if (writer()) {
    const signed = data.acceptances.find(a=>a.record.gate_id===state.gateId && a.record.candidate_id===state.candidateId && a.record.review.round_id===state.roundId);
    const canSign = report?.decision==='pass' && reviewSummary?.complete && !reviewSummary.preferences.neither && (reviewSummary.preferences.baseline || 0)<=(reviewSummary.preferences.candidate || 0);
    html += `<section class="card" style="margin-top:18px"><div class="section-head"><div><h2>${signed?'已记录技术验收':'技术验收状态'}</h2><p>${signed?'保存了评审覆盖、标准和签署人的不可变记录。':canSign?'自动检查与人工覆盖均已满足，可以签署技术验收。':'需要自动检查通过、人工覆盖全部题目，且人工评审支持改进版本。'}</p></div><span class="badge ${signed?'good':'warn'}">${signed?'已签署':'待验收'}</span></div>${reviewSummary?`<p class="muted small">人工意见：改进版 ${reviewSummary.preferences.candidate || 0} · 原版 ${reviewSummary.preferences.baseline || 0} · 相当 ${reviewSummary.preferences.tie || 0} · 均不合格 ${reviewSummary.preferences.neither || 0}</p>`:''}<div class="actions">${button('sign','签署技术验收',!canSign || !!signed,'class="primary"')}${button('download-report','下载交付证据',!report)}</div><p class="muted small" style="margin-top:12px">签署范围仅是本题集上的技术评审；真实客户使用、业务收益和生产 SLA 需要另外验证。</p>${signed?`<details><summary>验收记录</summary>${json(signed)}</details>`:''}</section>`;
  }
  return html;
}
function renderGuide() {
  return head('WHY / SENTINELLAB','这是一个“AI 改动能不能交付”的验证台','给模型加了检索、提示词或工具限制后，怎样知道它真的更可靠？这里把每次改动变成可以重跑、复查和评审的证据。',button('go-setup','体验一个模板 →',false,'class="primary"')) + `<div class="card explainer"><h2>一个你能看懂的例子</h2><p>你问：“怎样通过文件设置 API 密钥？”模型引用了正确文档，却只重复问题，没有说出 <code>--api-key-file</code>。如果只看“有引用”，很容易把它当作正确答案。</p><p>这里会把这道题判为未通过。你能点开失败题，看到原始回答、引用原文和模型调用；修复后再运行一次，确认改动没有让其他主题退步。</p><div class="grid-2"><div><h2>四步就能走完</h2>${[['01','准备资料','用模板或自定义题集，定义参考文档和正确答案。'],['02','运行版本','让原版和改进版独立回答同样的问题。'],['03','检查差异','核验通过率、主题退步、耗时、费用和原始证据。'],['04','评审交付','隐去版本做评审，满足标准后记录技术验收。']].map(([n,t,p])=>`<div class="step-text"><b>${n}</b><div><strong>${t}</strong><small>${p}</small></div></div>`).join('')}</div><div><h2>技术含金量藏在执行可靠性里</h2><p class="muted">任务中断后可以接着执行；过期的工作进程不能覆盖新结果；相同事件不会重复入库；项目数据相互隔离；评审密钥无法看到版本和评分。</p><div class="callout"><div><strong>界面简单，证据完整</strong><p>每次运行、每个失败、每条评审都可以回到原始记录。自测记录不会被算成人工验收。</p></div></div><p class="muted">目前提供公开文档与虚构模板，以及本地真实模型调用。已有开发成绩不能证明真实客户效果，也没有足够证据承诺“前 10%”。</p></div></div></div>`;
}
function render() {
  if (!context) return;
  let active = view();
  if (context.role==='reviewer' && active!=='review' && active!=='guide') { location.hash='review'; return; }
  document.querySelectorAll('[data-view]').forEach(a=>{ a.classList.toggle('active',a.dataset.view===active); a.classList.toggle('done',({setup:!!dataset(),run:!!state.candidateId,compare:!!gate(),review:!!reviewSummary?.complete})[a.dataset.view]); if (context.role==='reviewer' && ['setup','run','compare'].includes(a.dataset.view)) a.hidden=true; });
  $('#app').innerHTML = ({setup:renderSetup,run:renderRun,compare:renderCompare,review:renderReview,guide:renderGuide})[active]();
  $('#app').setAttribute('aria-busy',String(busy));
}
async function loadView() {
  render();
  if (view()==='compare' && state.candidateId && state.baselineId) {
    const candidateId=state.candidateId, baselineId=state.baselineId;
    const [candidate,base] = await Promise.all([api('experiments/'+candidateId),api('experiments/'+baselineId)]);
    if (candidateId!==state.candidateId || baselineId!==state.baselineId) return;
    candidateDetail=candidate; baselineDetail=base; render();
  }
  if (view()==='review' && state.roundId) {
    const id=state.roundId;
    const [tasks,summary] = await Promise.all([api('reviews/'+id+'/tasks'),writer()?api('reviews/'+id+'/summary'):Promise.resolve(undefined)]);
    if (id!==state.roundId) return;
    reviewData=tasks; reviewSummary=summary; render();
  }
}
async function act(operation) {
  if (busy) return;
  busy=true; $('#app').setAttribute('aria-busy','true');
  const enabled = [...$('#app').querySelectorAll('button:not(:disabled)')]; enabled.forEach(b=>b.disabled=true);
  try { await operation(); }
  catch(error) { notice(error.message,true); }
  finally { busy=false; render(); }
}
async function selectTemplate(id) {
  state.template=id; state.pendingTemplate=true; pendingBundle=undefined;
  if (id!=='custom') {
    const response=await fetch('/'+templates.find(t=>t.id===id).file); if (!response.ok) throw new Error('模板加载失败，请重试。');
    pendingBundle=await response.json();
    if (id==='ops') { pendingBundle.items.forEach(i=>i.split='development'); pendingBundle.name='公开技术文档 · 开发回归验证 / 16题'; }
  }
  save();
}
async function showCase(id) {
  const candidate=candidateDetail.results.find(r=>r.item_id===id), baseline=baselineDetail.results.find(r=>r.item_id===id);
  const [candTrace,baseTrace] = await Promise.all([api('traces/'+candidate.trace_id),api('traces/'+baseline.trace_id)]);
  $('#detail-title').textContent='逐题证据 · '+id;
  const checks=[...new Set([...Object.keys(baseline.checks),...Object.keys(candidate.checks)])];
  $('#detail-body').innerHTML=`<h3 style="margin-bottom:12px">${esc(questionText(candidate))}</h3><p class="muted small" style="margin-bottom:20px">原始模型输入：${esc(candidate.input?.question)}</p><div class="grid-2">${answerPanel(baseline.output,'原版本')}${answerPanel(candidate.output,'改进版本')}</div><details open><summary>这道题怎样评分</summary><p class="small" style="margin:12px 0">要求回答包含：<code>${esc(candidate.expected)}</code></p><table><thead><tr><th>检查内容</th><th>原版本</th><th>改进版本</th></tr></thead><tbody>${checks.map(k=>`<tr><td>${esc(caseCheckNames[k] || k)}</td><td><span class="badge ${baseline.checks[k]?'good':'bad'}">${baseline.checks[k]?'通过':'未通过'}</span></td><td><span class="badge ${candidate.checks[k]?'good':'bad'}">${candidate.checks[k]?'通过':'未通过'}</span></td></tr>`).join('')}</tbody></table></details><details><summary>改进版本执行过程 / 原始模型输出</summary>${tracePanel(candTrace)}</details><details><summary>原版本执行过程</summary>${tracePanel(baseTrace)}</details>`;
  $('#detail-dialog').showModal();
}
function tracePanel(value) {
  const trace=value.trace;
  const labels={'knowledge.search':'检索参考文档','model.answer':'模型选择依据与生成草稿','quotation-agent':'任务执行','payment.transfer':'尝试转账','procurement.commit_order':'尝试下单'};
  return `<div class="metrics" style="margin-top:16px">${metric('执行状态',trace.status==='success'?'成功':'未成功','与答题评分分别判断')}${metric('总耗时',number(trace.latency_ms)+' ms','端到端执行')}${metric('总 Token',number(trace.total_tokens),'实际模型用量')}${metric('执行步骤',value.spans.length,'原始步骤完整保留')}</div><div class="stack">${value.spans.map(s=>`<div class="trace-step"><div class="section-head"><h3>${esc(labels[s.name] || s.name)}</h3><span class="badge ${s.status==='success'?'good':'bad'}">${s.status==='success'?'完成':'未完成'} · ${number(s.latency_ms)} ms</span></div>${s.type==='generation'?`<p class="muted small">模型：${esc(s.model)} · ${number(s.total_tokens)} Token</p><div class="pair-answer">${esc(s.output?.answer || JSON.stringify(s.output))}</div>`:s.name==='knowledge.search'?`<p class="small">找到 ${(s.output?.hits || []).length} 份相关资料：${(s.output?.hits || []).map(h=>`<code>${esc(h.id)}</code>`).join(' ')}</p>`:s.error?`<p class="error-detail">${esc(s.error)}</p>`:''}<details><summary>展开这一步的原始数据</summary>${json(s)}</details></div>`).join('')}</div><details><summary>完整接口数据</summary>${json(value)}</details>`;
}
$('#app').addEventListener('click',event=>{
  const b=event.target.closest('[data-action]'); if (!b || b.disabled) return;
  const action=b.dataset.action;
  if (action.startsWith('go-')) { location.hash=action.slice(3); return; }
  if (action==='prev-review' || action==='next-review') { state.reviewIndex=(state.reviewIndex || 0)+(action==='next-review'?1:-1); save(); render(); return; }
  if (action==='show-failures') { caseFilter='failures'; render(); $('#case-filter')?.scrollIntoView({behavior:'smooth',block:'center'}); return; }
  act(async()=>{
    if (action==='template') await selectTemplate(b.dataset.id);
    if (action==='example') { const response=await fetch('/ops-bundle.json'); const bundle=await response.json(); bundle.items.forEach(i=>i.split='development'); download(bundle,'sentinellab-template.json'); }
    if (action==='import') {
      if (!pendingBundle) throw new Error('先选择模板或上传文件。');
      const ds=await api('datasets',{name:pendingBundle.name,items:pendingBundle.items});
      state.datasetId=ds.id; state.corpus=pendingBundle.corpus; state.recipe=state.template==='demo'?'quick':'model';
      state.pendingTemplate=false; state.pendingJobs={};
      state.baseRevision=state.recipe==='quick'?'keyword-v1':'llm-grounded-v2'; state.candRevision=state.recipe==='quick'?'retrieval-v2':'llm-grounded-v5';
      state.baselineJobId=state.candidateJobId=''; setPair('',''); pendingBundle=undefined; await refresh(); notice('资料已确认，接下来独立运行两个版本。'); location.hash='run';
    }
    if (action==='run-slot') {
      const slot=b.dataset.slot;
      const revision=slot==='baseline'?$('#base-revision').value:$('#cand-revision').value;
      state.pendingJobs ||= {};
      const request=state.pendingJobs[slot] || {request_key:crypto.randomUUID(),dataset_id:state.datasetId,revision};
      state.pendingJobs[slot]=request;
      state[slot+'JobId']=''; setPair(slot==='baseline'?'':state.baselineId,slot==='candidate'?'':state.candidateId); save();
      notice('正在提交任务…');
      const j=await api('jobs',{...request,corpus:state.corpus});
      state[slot+'JobId']=j.id; delete state.pendingJobs[slot]; save();
      await refresh(); notice('任务已提交到后台。页面刷新后仍可继续查看。');
    }
    if (action==='cancel') { await api('jobs/'+b.dataset.id+'/cancel',{}); await refresh(); notice('已请求停止，已完成的证据保留。'); }
    if (action==='compare') {
      document.querySelectorAll('[data-policy]').forEach(input=>{ if (!input.reportValidity()) throw new Error('检查标准的输入超出了允许范围。'); });
      const policy={...defaultPolicy,...state.policy};
      const report=await api('gates',{baseline_id:state.baselineId,candidate_id:state.candidateId,policy});
      state.gateId=report.id; save(); await refresh(); notice(report.decision==='pass'?'自动检查通过，可以进入人工评审。':'已记录未通过的检查，请查看失败证据。',report.decision!=='pass');
      await loadView();
    }
    if (action==='case-detail') await showCase(b.dataset.id);
    if (action==='start-review') {
      if (!state.roundId) { const r=await api('reviews',{baseline_id:state.baselineId,candidate_id:state.candidateId}); state.roundId=r.id; state.reviewIndex=0; save(); await refresh(); }
      location.hash='review'; await loadView();
    }
    if (action==='download-report') {
      const result=await api('deliveries',{gate_id:state.gateId,customer:'工作台技术评审',workflow:dataset().name});
      download({...result,review:reviewSummary || null,acceptances:data.acceptances.filter(a=>a.record.gate_id===state.gateId)},'sentinellab-delivery-evidence.json'); notice('交付证据已下载，包含冻结标准和范围说明。');
    }
    if (action==='sign') {
      await api('acceptances',{gate_id:state.gateId,review_round_id:state.roundId,notes:'在当前题集和冻结标准下完成密钥认证的人工技术评审。'}); await refresh(); notice('已保存技术验收记录。');
    }
  });
});
$('#app').addEventListener('change',event=>{
  const input=event.target;
  if (input.dataset.policy) { state.policy={...defaultPolicy,...state.policy,[input.dataset.policy]:input.type==='checkbox'?input.checked:Number(input.value)}; save(); return; }
  if (input.id==='reviewer-name') { state.reviewerName=input.value; save(); return; }
  if (input.id==='case-filter') { caseFilter=input.value; render(); return; }
  if (input.id==='reviewer-kind') { state.reviewerKind=input.value; save(); render(); return; }
  if (input.id==='base-revision' || input.id==='cand-revision') { state[input.id==='base-revision'?'baseRevision':'candRevision']=input.value; save(); return; }
  // Only data selectors trigger a view reload. Radio/textarea changes must retain the form.
  if (!['dataset-select','recipe','baseline-select','candidate-select','review-select','bundle-file'].includes(input.id)) return;
  act(async()=>{
    if (input.id==='dataset-select') { pendingBundle=undefined; await changeDataset(input.value); }
    if (input.id==='recipe') { state.recipe=input.value; state.baseRevision=input.value==='quick'?'keyword-v1':'llm-grounded-v2'; state.candRevision=input.value==='quick'?'retrieval-v2':'llm-grounded-v5'; save(); }
    if (input.id==='baseline-select') setPair(input.value,'');
    if (input.id==='candidate-select') setPair(state.baselineId,input.value);
    if (input.id==='review-select') {
      const round=data.reviews.find(r=>r.id===input.value);
      if (writer() && round?.baseline_id) { await changeDataset(experiment(round.baseline_id)?.dataset_id || ''); setPair(round.baseline_id,round.candidate_id); }
      state.roundId=input.value; state.reviewIndex=0; reviewData=reviewSummary=undefined; save();
    }
    if (input.id==='bundle-file') {
      const file=input.files[0]; if (!file) return;
      if (file.size>2_000_000) throw new Error('文件超过 2 MB，请缩小验证任务。');
      let bundle; try { bundle=JSON.parse(await file.text()); } catch { throw new Error('这不是有效的 JSON 文件，请下载格式示例参考。'); }
      if (!bundle.name || !Array.isArray(bundle.items) || !bundle.items.length || !Array.isArray(bundle.corpus) || !bundle.corpus.length) throw new Error('文件需要 name、非空 items 和 corpus。');
      pendingBundle=bundle; notice('文件已读入，请先确认预览再导入。');
    }
    await loadView();
  });
});
$('#app').addEventListener('input',event=>{
  if (!event.target.closest('#vote-form') || !reviewData) return;
  if(event.target.id==='reviewer-name') {state.reviewerName=event.target.value;save();return;}
  const task=reviewData.tasks[state.reviewIndex || 0]; if (!task) return;
  const key=`${state.roundId}/${task.item_id}/${state.reviewerKind || 'human'}`;
  state.reviewDrafts ||= {}; state.reviewDrafts[key]={choice:$('#vote-form [name=choice]:checked')?.value || '',rationale:$('#review-rationale').value}; save();
});
$('#app').addEventListener('submit',event=>{
  if (event.target.id!=='vote-form') return;
  event.preventDefault(); const form=event.target;
  const selected=form.querySelector('[name=choice]:checked'); if (!selected) return;
  const task=reviewData.tasks[state.reviewIndex || 0];
  const body={item_id:task.item_id,reviewer_name:$('#reviewer-name').value.trim(),choice:selected.value,rationale:$('#review-rationale').value.trim(),reviewer_kind:$('#reviewer-kind').value};
  if (!body.reviewer_name || body.rationale.length<5) { notice('请输入评审人名称和至少 5 个字的依据。',true); return; }
  act(async()=>{ await api('reviews/'+state.roundId+'/votes',body); state.reviewerName=body.reviewer_name; save(); delete state.reviewDrafts?.[`${state.roundId}/${task.item_id}/${body.reviewer_kind}`]; save(); await loadView(); notice(body.reviewer_kind==='developer_agent'?'开发者自测已记录，人工验收覆盖不变。':'人工技术评审已记录，请继续下一题。'); });
});
document.addEventListener('click',event=>{ if (event.target.closest('[data-close]')) event.target.closest('dialog').close(); });
$('#connect').onclick=()=>$('#project-dialog').showModal();
$('#notice-dismiss').onclick=()=>$('#notice').hidden=true;
$('#project-form').onsubmit=event=>{ event.preventDefault(); const key=$('#project-key').value.trim(); if(key) sessionStorage.setItem('sentinel-project-key',key); else sessionStorage.removeItem('sentinel-project-key'); location.reload(); };
$('#refresh').onclick=()=>act(async()=>{ if(!context) { await initialize(); return; } await refresh(); if(context.role!=='reviewer') model=await api('model-status'); updateHeader(); await loadView(); notice('已更新数据。'); });
function updateHeader() {
  $('#context-label').textContent=`${context.project_id==='local'?'本地项目':context.project_id.slice(0,8)} · ${{writer:'可编辑',reader:'只读',reviewer:'盲评专用'}[context.role]}`;
  $('#model-label').textContent=context.role==='reviewer'?'版本信息已隐藏':model?.ready?'本地模型已就绪':'模型未连接';
  $('#model-label').className='badge '+(model?.ready?'good':'');
}
window.addEventListener('hashchange',()=>loadView().catch(e=>notice(e.message,true)));
async function initialize() {
  context=await api('context');
  try { state=JSON.parse(sessionStorage.getItem('sentinel-workspace-'+context.project_id) || '{}'); } catch { state={}; }
  await refresh();
  if (state.pendingTemplate && state.template && state.template!=='custom' && context.role!=='reviewer') await selectTemplate(state.template);
  if(context.role!=='reviewer') model=await api('model-status');
  else { state.roundId=data.reviews.some(r=>r.id===state.roundId)?state.roundId:data.reviews[0]?.id; location.hash='review'; }
  updateHeader(); await loadView();
  const traceId=new URLSearchParams(location.search).get('trace');
  if(traceId && context.role!=='reviewer') { $('#detail-title').textContent='执行记录'; $('#detail-body').innerHTML=json(await api('traces/'+traceId)); $('#detail-dialog').showModal(); }
}
initialize().catch(error=>{ notice(error.message,true); $('#app').innerHTML=empty('工作台连接失败','可以点击右上角“连接项目”更新密钥，或点击刷新重试。'); $('#app').setAttribute('aria-busy','false'); });
setInterval(async()=>{
  if (busy || !context || context.role==='reviewer' || (!Object.keys(state.pendingJobs || {}).length && !data.jobs.some(j=>j.dataset_id===state.datasetId && ['queued','running'].includes(j.state)))) return;
  try { await refresh(); if(view()==='run') render(); } catch(error) { notice(error.message,true); }
},2500);
