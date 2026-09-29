import { useEffect, useRef, useState } from 'react';

import { ACTIVE, downloadUrl, FINISHED, type Job, type JobStatus } from '../api';
import { duration, pageList } from '../format';
import type { RunState } from '../run';

const STEPS = ['读取文件', '识别版面', '翻译', '排版', '生成文件'];

interface Props {
  job: Job;
  run: RunState;
  status: JobStatus;
  onCancel: () => Promise<void>;
  onRerun: (action: 'recompile' | 'retranslate') => Promise<void>;
  onHome: () => void;
  onRetry: () => void;
}

function chip(status: JobStatus, warnings: number): [string, string] {
  switch (status) {
    case 'queued':
      return ['run', '排队中'];
    case 'running':
      return ['run', '翻译中'];
    case 'recompile':
      return ['run', '重新编译中'];
    case 'done':
      return ['ok', '已完成'];
    case 'partial':
      return ['warn', `已完成 · ${warnings} 处提醒`];
    case 'failed':
      return ['err', '未完成'];
    case 'canceled':
      return ['', '已取消'];
  }
}

export function EventsCard({ job, run, status, onCancel, onRerun, onHome, onRetry }: Props) {
  const list = useRef<HTMLDivElement>(null);
  const [confirm, setConfirm] = useState(false);
  const [askRetranslate, setAskRetranslate] = useState(false);
  const [busy, setBusy] = useState(false);
  const finished = FINISHED.includes(status);
  const stats = job.stats ?? {};
  const warnings = stats.warnings ?? 0;

  useEffect(() => {
    const el = list.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [run.items.length]);
  useEffect(() => {
    setConfirm(false);
    setAskRetranslate(false);
  }, [status]);

  const rerun = async (action: 'recompile' | 'retranslate') => {
    setBusy(true);
    setAskRetranslate(false);
    await onRerun(action).finally(() => setBusy(false));
  };

  // 完成后阶段全亮；失败停在出错那一步
  const active = finished ? STEPS.length : status === 'queued' ? -1 : run.step;
  const failAt = run.fail ? run.step : -1;
  const [tone, label] = chip(status, warnings);
  const queue = status === 'queued' || status === 'recompile' ? (run.queue ?? job.queue) : null;

  let foot: React.ReactNode = null;
  if (ACTIVE.includes(status)) {
    foot = confirm ? (
      <div className="foot-row">
        <span className="t">
          <b>确定取消？</b>不计入今日额度
        </span>
        <span>
          <button className="btn btn-secondary sm" onClick={() => setConfirm(false)}>
            继续翻译
          </button>{' '}
          <button
            className="btn btn-danger sm"
            disabled={busy}
            onClick={async () => {
              setBusy(true);
              await onCancel().finally(() => setBusy(false));
            }}
          >
            确定取消
          </button>
        </span>
      </div>
    ) : (
      <div className="foot-row">
        <span className="t">不想翻这篇了？</span>
        <button className="btn btn-ghost danger" onClick={() => setConfirm(true)}>
          取消翻译
        </button>
      </div>
    );
  } else if (status === 'failed' || status === 'canceled') {
    foot = (
      <>
        <div className={`result${status === 'failed' ? ' err' : ''}`}>
          <div className="rt">{status === 'failed' ? '没有生成译文' : '已取消'}</div>
          <div className="rs">本次不计入今日额度</div>
        </div>
        <button className="btn btn-primary" style={{ width: '100%' }} onClick={onHome}>
          换一篇论文
        </button>
      </>
    );
  } else if (finished && job.final_rev) {
    const [title, sub] =
      status === 'partial'
        ? [
            `翻译完成，有 ${warnings} 处提醒`,
            `第 ${pageList(stats.warn_pages ?? [])} 页有部分内容保留原文，其余均已翻译`,
          ]
        : job.cache_hit
          ? ['翻译完成', '已从译文库直接加载 · 不占今日额度']
          : ['翻译完成', `共 ${job.pages} 页 · 用时 ${duration(stats.elapsed_s ?? 0)}`];
    foot = (
      <>
        <div className="result">
          <div className="rt">{title}</div>
          <div className="rs">{sub}</div>
        </div>
        <div className="dl">
          <a className="btn btn-primary" href={downloadUrl(job.id, 'translated')} download>
            下载译文
          </a>
          <a className="btn btn-secondary" href={downloadUrl(job.id, 'dual')} download>
            下载中英对照
          </a>
        </div>
        <div className="rerun">
          {askRetranslate ? (
            <div className="foot-row">
              <span className="t">
                <b>确定从头重新翻译？</b>会替换现有译文
              </span>
              <span>
                <button className="btn btn-secondary sm" onClick={() => setAskRetranslate(false)}>
                  先不了
                </button>{' '}
                <button className="btn btn-danger sm" disabled={busy} onClick={() => void rerun('retranslate')}>
                  重新翻译
                </button>
              </span>
            </div>
          ) : (
            <>
              <div className="dl">
                <button
                  className="btn btn-secondary sm"
                  disabled={busy}
                  title="沿用已有译文，重新排版并生成文件，不再调用模型"
                  onClick={() => void rerun('recompile')}
                >
                  重新编译
                </button>
                <button
                  className="btn btn-secondary sm"
                  disabled={busy}
                  title="丢掉现有译文，从头完整翻译一遍"
                  onClick={() => setAskRetranslate(true)}
                >
                  重新翻译
                </button>
              </div>
              <div className="rerun-hint">重新编译沿用已有译文重新排版；重新翻译从头来过</div>
            </>
          )}
        </div>
        {job.cache_hit && (
          <div className="foot-links">
            <button className="link" onClick={onRetry}>
              想要不同效果？换个思考强度重新翻译
            </button>
          </div>
        )}
      </>
    );
  }

  return (
    <aside className="card events">
      <div className="ev-head">
        <h2>处理进度</h2>
        <span className={`status ${tone}`}>{label}</span>
      </div>
      <div className="stepper">
        {STEPS.map((s, i) => (
          <div key={s} className={`step ${i === failAt ? 'fail' : i < active ? 'done' : i === active ? 'active' : ''}`}>
            {s}
          </div>
        ))}
      </div>
      {queue && (
        <div className="queue">
          <div className="q1">{queue.ahead > 0 ? `前面还有 ${queue.ahead} 篇` : '马上开始'}</div>
          {queue.ahead > 0 && (
            <div className="q2">预计 {Math.max(1, Math.round(queue.eta_seconds / 60))} 分钟后开始</div>
          )}
          <div className="q3">可以关闭页面或翻译别的论文，稍后点左上角图标在「历史翻译」查看</div>
        </div>
      )}
      <div className="ev-list" ref={list}>
        {run.items.map((e) => (
          <div key={e.key} className={`ev ${e.kind}`}>
            <time>{e.time}</time>
            <span className="txt">
              {e.text}
              {e.sub && <small>{e.sub}</small>}
            </span>
          </div>
        ))}
      </div>
      {foot && <div className="ev-foot">{foot}</div>}
    </aside>
  );
}
