import { useState } from 'react';

import { ACTIVE, type JobItem, type JobStatus } from '../api';
import { when } from '../format';
import { Icon, useDismiss } from './Topbar';

const LABEL: Record<JobStatus, [string, string]> = {
  queued: ['排队中', 'run'],
  running: ['翻译中', 'run'],
  recompile: ['排队中', 'run'],
  done: ['已完成', 'ok'],
  partial: ['有提醒', 'warn'],
  failed: ['未完成', 'err'],
  canceled: ['已取消', ''],
};

export function statusLabel(item: Pick<JobItem, 'status' | 'warnings'>): [string, string] {
  if (item.status === 'partial') return [`有 ${item.warnings} 处提醒`, 'warn'];
  return LABEL[item.status];
}

interface Props {
  items: JobItem[];
  current: string | null;
  onClose: () => void;
  onOpen: (id: string) => void;
  onNew: () => void;
  onDelete: (id: string) => Promise<void>;
}

export function HistoryDrawer({ items, current, onClose, onOpen, onNew, onDelete }: Props) {
  const [menu, setMenu] = useState<string | null>(null);
  const [gone, setGone] = useState<string | null>(null);
  let group = '';
  return (
    <>
      <div className="scrim" onClick={onClose} />
      <aside className="drawer" aria-label="历史翻译">
        <div className="dr-head">
          <h2>历史翻译</h2>
          <button className="icon-btn" onClick={onClose} aria-label="收起">
            <Icon id="x" />
          </button>
        </div>
        <div className="dr-new">
          <button className="btn btn-secondary" onClick={onNew}>新翻译</button>
        </div>
        <div className="dr-list">
          {items.length === 0 && <div className="dr-group">还没有翻译记录</div>}
          {items.map((item) => {
            const { group: g, label } = when(item.finished_at ?? item.created_at);
            const head = g !== group ? <div className="dr-group">{(group = g)}</div> : null;
            const [text, tone] = statusLabel(item);
            const active = ACTIVE.includes(item.status);
            return (
              <div key={item.id}>
                {head}
                <div
                  className={`hi${item.id === current ? ' cur' : ''}${gone === item.id ? ' gone' : ''}`}
                  onClick={() => onOpen(item.id)}
                >
                  <div className="b">
                    <div className="n">{item.filename}</div>
                    <div className="s">
                      <span className={`s ${tone}`}>{text}</span> · {label}
                    </div>
                    <div className="m">
                      {item.model_label} · {item.thinking}
                    </div>
                  </div>
                  {!active && (
                    <ItemMenu
                      open={menu === item.id}
                      setOpen={(o) => setMenu(o ? item.id : null)}
                      onDelete={async () => {
                        setMenu(null);
                        setGone(item.id);
                        await onDelete(item.id).finally(() => setGone(null));
                      }}
                    />
                  )}
                </div>
              </div>
            );
          })}
        </div>
        <div className="dr-note">只显示你本人的记录；删除后不影响其他人。</div>
      </aside>
    </>
  );
}

function ItemMenu({ open, setOpen, onDelete }: { open: boolean; setOpen: (o: boolean) => void; onDelete: () => void }) {
  const ref = useDismiss<HTMLDivElement>(open, () => setOpen(false));
  return (
    <div ref={ref} onClick={(e) => e.stopPropagation()}>
      <button className={`icon-btn more${open ? ' open' : ''}`} aria-label="更多" onClick={() => setOpen(!open)}>
        <Icon id="dots" />
      </button>
      <div className={`menu${open ? ' open' : ''}`}>
        <button className="menu-item danger" onClick={onDelete}>
          删除记录<span className="sub">只删除你的记录，不影响他人</span>
        </button>
      </div>
    </div>
  );
}
