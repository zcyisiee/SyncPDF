import { useEffect, useRef, useState } from 'react';

import type { Me } from '../api';

/** 设计稿里的 SVG symbol，全局只挂一次。 */
export function IconDefs() {
  return (
    <svg width="0" height="0" style={{ position: 'absolute' }} aria-hidden="true">
      <symbol id="i-down" viewBox="0 0 24 24"><path d="M6 9l6 6 6-6" /></symbol>
      <symbol id="i-x" viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18" /></symbol>
      <symbol id="i-dots" viewBox="0 0 24 24"><path d="M6 12h.01M12 12h.01M18 12h.01" strokeWidth="3" /></symbol>
      <symbol id="i-logo" viewBox="0 0 28 28">
        <rect x="3" y="5" width="13" height="18" rx="3" fill="#141413" stroke="none" />
        <rect x="12" y="5" width="13" height="18" rx="3" fill="#FAF9F5" stroke="#141413" strokeWidth="1.8" />
        <path d="M6.5 10h6M6.5 13.5h6M6.5 17h4" stroke="#FAF9F5" strokeWidth="1.6" />
        <path d="M15.5 10h6M15.5 13.5h6M15.5 17h4" stroke="#141413" strokeWidth="1.6" />
      </symbol>
    </svg>
  );
}

export const Icon = ({ id, className = 'icon' }: { id: string; className?: string }) => (
  <svg className={className}><use href={`#i-${id}`} /></svg>
);

/** 点在 ref 外面时关闭（菜单通用）。 */
export function useDismiss<T extends HTMLElement>(open: boolean, close: () => void) {
  const ref = useRef<T>(null);
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) close();
    };
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && close();
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [open, close]);
  return ref;
}

interface Props {
  me: Me | null;
  onDrawer: () => void;
  onHome: () => void;
  onLogout: () => void;
}

export function Topbar({ me, onDrawer, onHome, onLogout }: Props) {
  const [open, setOpen] = useState(false);
  const ref = useDismiss<HTMLDivElement>(open, () => setOpen(false));
  return (
    <header className="topbar">
      {me && (
        <button className="icon-btn logo-btn" onClick={onDrawer} title="历史翻译" aria-label="打开历史翻译">
          <Icon id="logo" className="brand-mark" />
        </button>
      )}
      <button className="brand" onClick={onHome} title="回到首页">
        <span className="brand-name">镜译</span>
        <span className="brand-sub">SyncTranslate</span>
      </button>
      <div className="spacer" />
      {me && (
        <>
          <span className="quota">
            <span className="lbl">今日剩余 </span>
            <b>{me.remaining}</b> 篇
          </span>
          <div className="account" ref={ref}>
            <button className="avatar" aria-label="账户" onClick={() => setOpen(!open)}>
              {me.name.slice(0, 2).toUpperCase()}
            </button>
            <div className={`menu${open ? ' open' : ''}`}>
              <div className="menu-head">
                <div className="who">{me.name}</div>
                <div className="meta">邀请码 {me.code}</div>
                <div className="meta">
                  今日额度 {me.remaining} / {me.daily_quota} 篇，每天 0 点恢复
                </div>
              </div>
              <button
                className="menu-item"
                onClick={() => {
                  setOpen(false);
                  onLogout();
                }}
              >
                退出登录
              </button>
            </div>
          </div>
        </>
      )}
    </header>
  );
}
