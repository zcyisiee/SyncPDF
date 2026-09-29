import { useRef, useState, type DragEvent } from 'react';

import { ACTIVE, upload, type ApiError, type Job, type JobItem, type Model } from '../api';
import { size, when } from '../format';
import { statusLabel } from './HistoryDrawer';

export const THINKING = ['low', 'medium', 'high'] as const;
const EFFORT_LABEL: Record<string, string> = { low: '快速', medium: '均衡', high: '细致' };
const MAX_BYTES = 50 << 20;
const RECENT = 4;

/** 一次翻译用的配置：harness / provider / model id / reasoning_effort。 */
export function Spec({ harness, provider, modelId, effort }: { harness: string; provider: string; modelId: string; effort: string }) {
  const rows: [string, string][] = [
    ['harness', harness],
    ['provider', provider],
    ['model id', modelId],
    ['reasoning_effort', effort],
  ];
  return (
    <dl className="spec">
      {rows.map(([k, v]) => (
        <div key={k}>
          <dt>{k}</dt>
          <dd>{v}</dd>
        </div>
      ))}
    </dl>
  );
}

interface Props {
  models: Model[];
  items: JobItem[];
  remaining: number;
  file: File | null;
  model: string;
  thinking: string;
  onFile: (file: File | null) => void;
  onModel: (value: string) => void;
  onThinking: (value: string) => void;
  onOpen: (id: string) => void;
  onHistory: () => void;
  onStarted: (job: Job, file: File) => void;
  toast: (message: string) => void;
}

export function Home(props: Props) {
  const { models, items, remaining, file, model, thinking, onFile, onModel, onThinking, onOpen, onHistory, onStarted, toast } =
    props;
  const input = useRef<HTMLInputElement>(null);
  const [drag, setDrag] = useState(false);
  const [progress, setProgress] = useState<{ loaded: number; total: number } | null>(null);
  const abort = useRef<(() => void) | null>(null);
  const uploading = progress !== null;
  const spec = models.find((m) => m.key === model);

  const pick = (picked: File | undefined) => {
    if (!picked) return;
    if (!/\.pdf$/i.test(picked.name) && picked.type !== 'application/pdf') return toast('只支持 PDF 文件');
    if (picked.size > MAX_BYTES) return toast('单篇不超过 50 MB');
    onFile(picked);
  };

  const start = async () => {
    if (!file) return;
    setProgress({ loaded: 0, total: file.size });
    const task = upload(file, { model, thinking }, (loaded, total) => setProgress({ loaded, total }));
    abort.current = task.abort;
    try {
      const job = await task.promise;
      onStarted(job, file);
    } catch (e) {
      const error = e as ApiError;
      if (error.code !== 'aborted') toast(error.message);
    } finally {
      abort.current = null;
      setProgress(null);
    }
  };

  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setDrag(false);
    pick(e.dataTransfer.files[0]);
  };

  const fraction = progress ? progress.loaded / Math.max(1, progress.total) : 0;
  return (
    <main className="view v-home home">
      <div className="hero">
        <h1>
          镜译 <span>SyncTranslate</span>
        </h1>
        <p>无损翻译学术论文，公式、排版、链接原样保留</p>
      </div>
      <section className="new-card">
        <div className="nc-step">
          <h2>
            <span className="num">1</span>选择论文
          </h2>
          <input
            ref={input}
            type="file"
            accept="application/pdf,.pdf"
            hidden
            onChange={(e) => {
              pick(e.target.files?.[0]);
              e.target.value = '';
            }}
          />
          {!file ? (
            <div
              className={`dropzone${drag ? ' drag' : ''}`}
              onDragEnter={(e) => (e.preventDefault(), setDrag(true))}
              onDragOver={(e) => (e.preventDefault(), setDrag(true))}
              onDragLeave={(e) => (e.preventDefault(), setDrag(false))}
              onDrop={onDrop}
            >
              <span className="t1">把 PDF 拖到这里，或</span>
              <button className="btn btn-secondary" onClick={() => input.current?.click()}>
                选择文件
              </button>
              <span className="t2">仅支持文字版 PDF · 单篇不超过 50 MB · 不超过 60 页</span>
            </div>
          ) : (
            <div className="picked">
              <div className="file-row">
                <span className="ext">PDF</span>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div className="name">{file.name}</div>
                  <div className="sub">
                    {!uploading
                      ? size(file.size)
                      : fraction < 1
                        ? `正在上传 · ${size(progress.loaded)} / ${size(file.size)}`
                        : '上传完成，正在检查文件'}
                  </div>
                </div>
                <button
                  className="btn btn-ghost"
                  onClick={() => (uploading ? abort.current?.() : input.current?.click())}
                >
                  {uploading ? '取消' : '更换'}
                </button>
              </div>
              {uploading && (
                <div className="bar">
                  <i style={{ width: `${fraction * 100}%` }} />
                </div>
              )}
            </div>
          )}
        </div>

        <div className={`nc-step${uploading ? ' locked' : ''}`}>
          <h2>
            <span className="num">2</span>选择翻译模型
          </h2>
          <div className="models" role="radiogroup" aria-label="模型">
            {models.map((m) => (
              <button
                key={m.key}
                role="radio"
                aria-checked={m.key === model}
                className={`model${m.key === model ? ' on' : ''}`}
                onClick={() => onModel(m.key)}
              >
                <span className="ml">{m.label}</span>
                <span className="ms">
                  {m.harness} · {m.provider}
                </span>
              </button>
            ))}
          </div>
          <div className="effort">
            <span className="lbl">思考强度</span>
            <div className="seg">
              {THINKING.map((level) => (
                <button
                  key={level}
                  className={level === thinking ? 'on' : ''}
                  disabled={!spec?.efforts.includes(level)}
                  onClick={() => onThinking(level)}
                >
                  {EFFORT_LABEL[level]} <small>{level}</small>
                </button>
              ))}
            </div>
            <span className="note">
              {spec && spec.efforts.length === 1
                ? `该模型的思考强度固定为 ${spec.efforts[0]}`
                : '越细致译文越讲究，耗时也越长'}
            </span>
          </div>
          {spec && <Spec harness={spec.harness} provider={spec.provider} modelId={spec.model_id} effort={thinking} />}
        </div>

        <div className="nc-foot">
          <button className="btn btn-primary start" disabled={!file || uploading || !spec} onClick={start}>
            开始翻译
          </button>
          <div className="nc-hint">
            {file ? `今日还可翻译 ${remaining} 篇 · 已有相同译文时直接加载，不占额度` : '先在第 1 步选择一篇 PDF'}
          </div>
        </div>
      </section>

      {items.length > 0 && (
        <section className="recent">
          <div className="rc-head">
            <h2>最近翻译</h2>
            {items.length > RECENT && (
              <button className="link" onClick={onHistory}>
                全部 {items.length} 篇
              </button>
            )}
          </div>
          {items.slice(0, RECENT).map((item) => {
            const [text, tone] = statusLabel(item);
            const active = ACTIVE.includes(item.status);
            return (
              <button key={item.id} className={`rc-item${active ? ' active' : ''}`} onClick={() => onOpen(item.id)}>
                <span className="b">
                  <span className="n">{item.filename}</span>
                  <span className="s">
                    <span className={`s ${tone}`}>{text}</span> · {item.model_label} · {item.thinking} ·{' '}
                    <span className="t">{when(item.finished_at ?? item.created_at).label}</span>
                  </span>
                </span>
                <span className={active ? 'btn btn-secondary sm' : 'go'}>{active ? '查看进度' : '打开'}</span>
              </button>
            );
          })}
        </section>
      )}
    </main>
  );
}
