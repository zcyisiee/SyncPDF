/**
 * 底部面板：事件流（当前论文，按页分组）、问题、引擎日志。点击条目跳到对应块或页。
 */
import { useLayoutEffect, useRef, type ReactNode } from 'react';
import type { IssueRecord } from '@shared/library';
import type { EngineEvent } from '@shared/protocol';
import { useLibrary } from '@/store/library';
import { useWorkbench, type PanelTab } from '@/store/workbench';
import { Panel, PanelTabs } from '@/layout/Panel';
import { describeEvent, type EventLine } from './describeEvent';

const EMPTY_EVENTS: EngineEvent[] = [];
/** 事件流最多渲染的条数（更早的仍保留在内存里）。 */
const RENDER_LIMIT = 1500;

export function BottomPanel(): JSX.Element {
  const tab = useWorkbench((s) => s.panelTab);
  const setTab = useWorkbench((s) => s.setPanelTab);
  const issueCount = useLibrary((s) => s.open?.issues.length ?? 0);
  return (
    <Panel className="sp-bottom">
      <PanelTabs
        tabs={[
          { id: 'events' as PanelTab, label: '事件流' },
          { id: 'issues' as PanelTab, label: '问题', badge: issueCount },
          { id: 'logs' as PanelTab, label: '引擎日志' },
        ]}
        active={tab}
        onSelect={setTab}
        actions={
          <button
            type="button"
            className="sp-icon-button"
            title="关闭面板 (⌘J)"
            onClick={() => useWorkbench.getState().togglePanel()}
          >
            <i className="codicon codicon-close" />
          </button>
        }
      />
      {tab === 'events' && <EventStream />}
      {tab === 'issues' && <Issues />}
      {tab === 'logs' && <Logs />}
    </Panel>
  );
}

/** 贴底滚动：用户停在底部时新内容到达自动跟随。 */
function FollowScroll({ children, deps }: { children: ReactNode; deps: unknown }): JSX.Element {
  const ref = useRef<HTMLDivElement | null>(null);
  const atBottom = useRef(true);
  useLayoutEffect(() => {
    const node = ref.current;
    if (node !== null && atBottom.current) node.scrollTop = node.scrollHeight;
  }, [deps]);
  return (
    <div
      ref={ref}
      className="sp-panel-body sp-list"
      onScroll={(event) => {
        const node = event.currentTarget;
        atBottom.current = node.scrollHeight - node.scrollTop - node.clientHeight < 24;
      }}
    >
      {children}
    </div>
  );
}

function jump(line: Pick<EventLine, 'page' | 'paragraphId'>): void {
  const state = useLibrary.getState();
  if (line.paragraphId !== null && state.open?.paragraphs[line.paragraphId] !== undefined) {
    state.select(line.paragraphId, true);
  } else if (line.page !== null) {
    state.revealPage(line.page);
  }
}

function EventStream(): JSX.Element {
  const events = useLibrary((s) => (s.openId === null ? EMPTY_EVENTS : (s.timeline[s.openId] ?? EMPTY_EVENTS)));
  const shown = events.slice(-RENDER_LIMIT);
  let lastPage: number | null = null;
  const rows: ReactNode[] = [];
  for (const event of shown) {
    const line = describeEvent(event);
    if (line.page !== null && line.page !== lastPage) {
      rows.push(
        <div key={`h${event.seq}`} className="sp-group">
          第 {line.page} 页
        </div>,
      );
    }
    if (line.page !== null) lastPage = line.page;
    const clickable = line.page !== null || line.paragraphId !== null;
    rows.push(
      <button
        key={event.seq}
        type="button"
        className={`sp-row tone-${line.tone}`}
        disabled={!clickable}
        onClick={() => jump(line)}
      >
        <span className="sp-row-time">{new Date(event.ts * 1000).toLocaleTimeString('zh-CN', { hour12: false })}</span>
        <i className={`codicon codicon-${line.icon}`} />
        <span className="sp-row-text">{line.text}</span>
      </button>,
    );
  }
  return (
    <FollowScroll deps={events}>
      {rows.length === 0 ? <p className="sp-hint">打开论文并开始翻译后，这里实时显示引擎事件。</p> : rows}
    </FollowScroll>
  );
}

function Issues(): JSX.Element {
  const issues = useLibrary((s) => s.open?.issues);
  if (issues === undefined || issues.length === 0) {
    return (
      <div className="sp-panel-body">
        <p className="sp-hint">没有问题。</p>
      </div>
    );
  }
  return (
    <div className="sp-panel-body sp-list">
      {issues.map((issue: IssueRecord, index) => (
        <button
          key={index}
          type="button"
          className={`sp-row tone-${issue.severity === 'info' ? 'muted' : issue.severity}`}
          onClick={() => jump({ page: issue.page, paragraphId: issue.paragraph_id })}
        >
          <i className={`codicon codicon-${issue.severity}`} />
          <span className="sp-row-text">
            <strong>{issue.code}</strong> {issue.message}
          </span>
          <span className="sp-row-where">
            {issue.paragraph_id ?? ''}
            {issue.page !== null && ` · 第 ${issue.page} 页`}
          </span>
        </button>
      ))}
    </div>
  );
}

function Logs(): JSX.Element {
  const logs = useLibrary((s) => s.logs);
  return (
    <FollowScroll deps={logs}>
      <pre className="sp-logs sp-selectable">{logs.length === 0 ? '（暂无日志）' : logs.join('\n')}</pre>
    </FollowScroll>
  );
}
