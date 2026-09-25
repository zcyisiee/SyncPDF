import { useState, type FormEvent } from 'react';

import { api, type ApiError } from '../api';

const QR = (
  <svg className="qr" viewBox="0 0 64 64">
    <rect width="64" height="64" rx="8" fill="#EDEAE0" />
    <g fill="#141413">
      <rect x="6" y="6" width="16" height="16" rx="2" /><rect x="42" y="6" width="16" height="16" rx="2" />
      <rect x="6" y="42" width="16" height="16" rx="2" /><rect x="28" y="10" width="6" height="6" />
      <rect x="28" y="28" width="8" height="8" /><rect x="42" y="30" width="6" height="6" />
      <rect x="50" y="42" width="8" height="6" /><rect x="30" y="46" width="6" height="12" />
      <rect x="42" y="50" width="6" height="8" /><rect x="10" y="30" width="10" height="6" />
    </g>
  </svg>
);

export function Login({ onLoggedIn }: { onLoggedIn: () => void }) {
  const [code, setCode] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!code.trim()) return setError('请输入邀请码');
    setBusy(true);
    try {
      await api.login(code.trim());
      onLoggedIn();
    } catch (err) {
      setError((err as ApiError).message);
      setBusy(false);
    }
  };

  return (
    <main className="view v-login login">
      <form className="login-card" onSubmit={submit}>
        <h1>镜译 SyncTranslate</h1>
        <p className="tagline">无损翻译学术论文，公式、排版、链接原样保留</p>
        <label className="field-label" htmlFor="invite">邀请码</label>
        <input
          className="input"
          id="invite"
          placeholder="YJ-XXXX-XXXX"
          autoComplete="off"
          value={code}
          onChange={(e) => {
            setCode(e.target.value);
            setError('');
          }}
        />
        <div className="hint" style={error ? { color: 'var(--err-ink)' } : undefined}>
          {error || '内测期间凭邀请码使用，每个邀请码对应一个账户'}
        </div>
        <button className="btn btn-primary" type="submit" disabled={busy}>
          进入镜译
        </button>
        <div className="divider">其他方式</div>
        <div className="wechat" aria-disabled="true">
          {QR}
          <div>
            <div className="t">
              微信扫码登录<span className="soon">即将开放</span>
            </div>
            <div className="hint" style={{ marginTop: 4 }}>开放后可直接扫码，历史记录会随账户保留</div>
          </div>
        </div>
      </form>
    </main>
  );
}
