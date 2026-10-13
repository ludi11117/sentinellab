const friendly = {
  'experiments must use the same dataset snapshot and evaluator version': '请选择同一题集、同一评分规则下的两个独立版本。',
  'automated gate must pass and all review items must be covered': '尚不能签署：自动检查必须通过，且人工评审必须覆盖每一道题。开发者自测不计入人工覆盖。',
  'human review does not support candidate acceptance': '人工评审发现不可接受的答案或更支持旧版本，请先修复。',
  'invalid or revoked project key': '项目密钥无效或已撤销，请重新连接项目。',
  'configured model is unavailable; run start-model.ps1 or configure the provider': '模型服务尚未就绪。请在项目目录运行 start-model.ps1，或使用快速演示模板。',
  'this key already submitted an immutable vote for this item': '这道题已提交评审，记录不可覆盖。可继续评审下一题。',
};

export async function api(path, body) {
  const headers = {};
  const key = sessionStorage.getItem('sentinel-project-key');
  if (key) headers.Authorization = 'Bearer ' + key;
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const response = await fetch('/api/public/' + path, {headers, ...(body === undefined ? {} : {method: 'POST', body: JSON.stringify(body)})});
  const value = await response.json();
  if (!response.ok) {
    const detail = typeof value.detail === 'string' ? value.detail : JSON.stringify(value.detail || value);
    throw new Error(friendly[detail] || `请求失败（${response.status}）：${detail}`);
  }
  return value;
}

export const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
export const percent = value => value == null ? '—' : (value * 100).toFixed(1) + '%';
export const number = value => value == null ? '—' : Number(value).toLocaleString('zh-CN', {maximumFractionDigits: 1});
export const json = value => `<pre>${esc(JSON.stringify(value, null, 2))}</pre>`;
export function download(value, filename) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], {type:'application/json'}));
  const a = document.createElement('a'); a.href = url; a.download = filename; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
