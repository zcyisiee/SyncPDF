/* API 访问层：所有请求带 token，统一解 {ok,data}/错误信封。只读 GET。 */
'use strict';

const TOKEN = new URLSearchParams(location.search).get('token') || '';

function withToken(path) {
  const sep = path.includes('?') ? '&' : '?';
  return `${path}${sep}token=${encodeURIComponent(TOKEN)}`;
}

async function getJson(path) {
  const res = await fetch(withToken(path), { cache: 'no-store' });
  if (!res.ok) {
    let message = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      if (body && body.error && body.error.message) message = body.error.message;
    } catch (err) { /* non-JSON error body */ }
    const error = new Error(message);
    error.status = res.status;
    throw error;
  }
  const payload = await res.json();
  if (payload && payload.ok === false) {
    const error = new Error((payload.error && payload.error.message) || '请求失败');
    error.status = res.status;
    throw error;
  }
  return payload;
}

export const api = {
  token: TOKEN,
  url: withToken,
  runs: () => getJson('/api/v1/runs'),
  manifest: (run) => getJson(`/api/v1/runs/${run}/manifest`),
  events: (run, afterSeq) =>
    getJson(`/api/v1/runs/${run}/events?after_seq=${afterSeq || 0}`),
  /** ``rel`` 相对 snapshots/（不含前缀），如 ``parse/layout.json``。404 → null。 */
  snapshot: async (run, rel) => {
    try {
      return await getJson(`/api/v1/runs/${run}/snapshot/${rel}`);
    } catch (err) {
      if (err.status === 404) return null;
      throw err;
    }
  },
  /** ``rel`` 含 ``artifacts/`` 前缀（事件里的 artifact 引用原样传入）。 */
  artifactUrl: (run, rel) => withToken(`/api/v1/artifacts/${run}/${rel}`),
  artifactJson: async (run, rel) => {
    try {
      return await getJson(`/api/v1/artifacts/${run}/${rel}`);
    } catch (err) {
      if (err.status === 404) return null;
      throw err;
    }
  },
  artifactText: async (run, rel) => {
    const res = await fetch(withToken(`/api/v1/artifacts/${run}/${rel}`), { cache: 'no-store' });
    if (res.status === 404) return null;
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.text();
  },
  renderUrl: (run, pdf, page, dpi) =>
    withToken(
      `/api/v1/runs/${run}/render/${page}.png?pdf=${encodeURIComponent(pdf)}&dpi=${dpi || 110}`
    ),
  heartbeat: () => getJson('/api/v1/heartbeat'),
};
