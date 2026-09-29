import { useEffect, useRef, useState } from 'react';

import { FINISHED, pageUrl, type Box, type Job, type JobStatus } from '../api';
import { size } from '../format';
import type { RunState } from '../run';

type Mode = 'orig' | 'trans' | 'dual';
const reduced = () => matchMedia('(prefers-reduced-motion: reduce)').matches;

interface Props {
  job: Job;
  run: RunState;
  status: JobStatus;
}

export function Preview({ job, run, status }: Props) {
  const [mode, setMode] = useState<Mode>('trans');
  const [pageNo, setPageNo] = useState(1);
  const pages = useRef<HTMLDivElement>(null);
  const scrolledAt = useRef(0);
  const finished = FINISHED.includes(status) && job.final_rev !== null;

  // 当前页码：视口中线穿过的那一页
  useEffect(() => {
    const root = pages.current;
    if (!root) return;
    const io = new IntersectionObserver(
      (entries) => {
        for (const e of entries) if (e.isIntersecting) setPageNo(Number((e.target as HTMLElement).dataset.page));
      },
      { root, rootMargin: '-45% 0px -45% 0px' },
    );
    root.querySelectorAll('.page-row').forEach((row) => io.observe(row));
    return () => io.disconnect();
  }, [job.id, job.pages, status === 'failed']);

  // 新完成的页滚到眼前；用户 4 秒内自己滚过就不打扰
  useEffect(() => {
    const root = pages.current;
    if (!run.follow || !root || Date.now() - scrolledAt.current < 4000) return;
    const row = root.querySelector<HTMLElement>(`.page-row[data-page="${run.follow.page}"]`);
    if (row) root.scrollTo({ top: row.offsetTop - 16 });
  }, [run.follow]);

  const total = job.pages;
  const done = finished ? total : status === 'running' ? run.done : null;
  const failed = status === 'failed';
  return (
    <section className="card preview">
      <div className="pv-head">
        <div className="pv-title">
          <div className="name">{job.filename}</div>
          <div className="sub">
            {size(job.size)} · {total} 页 · {job.model.replace(/^.*\//, '')} · {job.thinking}
          </div>
        </div>
        {!failed && (
          <div className="seg">
            {(['orig', 'trans', 'dual'] as const).map((m) => (
              <button key={m} className={mode === m ? 'on' : ''} onClick={() => setMode(m)}>
                {{ orig: '原文', trans: '译文', dual: '对照' }[m]}
              </button>
            ))}
          </div>
        )}
        <span className="pageno">{failed ? '' : `第 ${pageNo} / ${total} 页`}</span>
      </div>
      {failed ? (
        <div className="pv-empty">
          <div>
            <h3>没有生成译文</h3>
            <p>处理中出现错误，本次不计入今日额度，可以稍后重新上传再试。</p>
          </div>
        </div>
      ) : (
        <>
          <div className="pv-progress">
            <span>
              {status === 'queued' || status === 'recompile' ? (
                '排队中，尚未开始翻译'
              ) : status === 'canceled' ? (
                '已取消'
              ) : (
                <>
                  已完成 <b>{done ?? 0}/{total}</b> 页
                </>
              )}
            </span>
            <div className={`bar${done === total ? ' done' : ''}`}>
              <i style={{ width: `${((done ?? 0) / total) * 100}%` }} />
            </div>
          </div>
          <div
            className={`pages m-${mode}`}
            ref={pages}
            onWheel={() => (scrolledAt.current = Date.now())}
            onTouchMove={() => (scrolledAt.current = Date.now())}
            onKeyDown={() => (scrolledAt.current = Date.now())}
          >
            {Array.from({ length: total }, (_, i) => i + 1).map((n) => {
              const live = run.pages[n];
              const [w, h] = job.page_sizes[n - 1] ?? [612, 792];
              const zh = finished
                ? pageUrl(job.id, n, 'tr', job.final_rev ?? undefined)
                : status === 'running' && live
                  ? pageUrl(job.id, n, 'tr', live.rev)
                  : null;
              const boxes = finished ? (job.stats?.boxes?.[String(n)] ?? []) : (live?.boxes ?? []);
              const anim = live?.anim || run.flipAll;
              const pill = status === 'running' && !live ? (run.step >= 2 ? 'run' : 'wait') : null;
              return (
                <Page
                  key={n}
                  n={n}
                  root={pages}
                  ar={`${w} / ${h}`}
                  en={pageUrl(job.id, n, 'src')}
                  zh={zh}
                  boxes={boxes}
                  anim={anim}
                  duration={live?.anim ? 1200 : 1000}
                  pill={pill}
                />
              );
            })}
          </div>
        </>
      )}
    </section>
  );
}

interface PageProps {
  n: number;
  root: React.RefObject<HTMLDivElement | null>;
  ar: string;
  en: string;
  zh: string | null;
  boxes: Box[];
  anim: number;
  duration: number;
  pill: 'run' | 'wait' | null;
}

function Page({ n, root, ar, en, zh, boxes, anim, duration, pill }: PageProps) {
  const row = useRef<HTMLDivElement>(null);
  const sheet = useRef<HTMLDivElement>(null);
  const [near, setNear] = useState(false);
  const [shown, setShown] = useState<string | null>(null);
  const [state, setState] = useState<'en' | 'anim' | 'zh'>('en');
  const [fade, setFade] = useState(false);
  const played = useRef(0);

  // 只渲染视口附近的页，60 页的论文也只拉眼前几张图
  useEffect(() => {
    const el = row.current;
    if (!el) return;
    const io = new IntersectionObserver(([e]) => e.isIntersecting && setNear(true), {
      root: root.current,
      rootMargin: '1200px 0px',
    });
    io.observe(el);
    return () => io.disconnect();
  }, [root]);

  useEffect(() => {
    if (!near) return;
    if (!zh) {
      setShown(null);
      setState('en');
      return;
    }
    const flip = () => {
      played.current = anim;
      if (reduced()) {
        setState('zh');
        setFade(true);
        return;
      }
      const el = sheet.current;
      setState('anim');
      el?.style.setProperty('--p', '-8%');
      const t0 = performance.now();
      const ease = (x: number) => (x < 0.5 ? 2 * x * x : 1 - (-2 * x + 2) ** 2 / 2);
      const step = (now: number) => {
        const k = Math.min(1, (now - t0) / duration);
        el?.style.setProperty('--p', `${-8 + 116 * ease(k)}%`);
        if (k < 1) requestAnimationFrame(step);
        else setState('zh');
      };
      requestAnimationFrame(step);
    };
    const wantsFlip = anim !== 0 && played.current !== anim;
    if (zh === shown) {
      if (wantsFlip) flip();
      return;
    }
    // 先解码再换图：同一页重发新 rev 时不会闪回原文
    let canceled = false;
    const img = new Image();
    img.src = zh;
    img.decode().then(
      () => {
        if (canceled) return;
        setShown(zh);
        if (wantsFlip) flip();
        else setState((s) => (s === 'anim' ? s : 'zh'));
      },
      () => {},
    );
    return () => {
      canceled = true;
    };
  }, [near, zh, anim, shown, duration]);

  const style = { '--ar': ar } as React.CSSProperties;
  return (
    <div className="page-row" data-page={n} ref={row}>
      <div className="sheet orig" style={style}>
        {near && <img src={en} alt={`第 ${n} 页原文`} />}
      </div>
      <div
        className={`sheet trans${fade ? ' fadein' : ''}`}
        data-s={state}
        ref={sheet}
        style={{ ...style, '--p': '0%' } as React.CSSProperties}
      >
        {near && <img className="en" src={en} alt="" />}
        {shown && <img className="zh" src={shown} alt={`第 ${n} 页译文`} />}
        <div className="sweep" />
        {boxes.map(([l, t, w, h], i) => (
          <div
            key={i}
            className="fb-box"
            style={{ left: `${l * 100}%`, top: `${t * 100}%`, width: `${w * 100}%`, height: `${h * 100}%` }}
          />
        ))}
        {boxes.length > 0 && (
          <span className="pill fb" title="这些段落暂时保留原文，稍后通过动态编译补上译文">
            等待动态编译 · {boxes.length} 段
          </span>
        )}
        {pill && <span className={`pill st${pill === 'run' ? ' run' : ''}`}>{pill === 'run' ? '正在翻译' : '等待翻译'}</span>}
      </div>
    </div>
  );
}
