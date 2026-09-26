import { useRef, useState, type DragEvent } from 'react';

import { upload, type ApiError, type Job, type JobItem } from '../api';
import { size } from '../format';
import { Icon } from './Topbar';

export const MODELS = ['gemini-3.8-flash'];
export const THINKING = ['low', 'medium', 'high'] as const;
const MAX_BYTES = 50 << 20;

interface Props {
  running: JobItem | null;
  file: File | null;
  thinking: string;
  onFile: (file: File | null) => void;
  onThinking: (value: string) => void;
  onResume: (id: string) => void;
  onStarted: (job: Job, file: File) => void;
  toast: (message: string) => void;
}

export function Home({ running, file, thinking, onFile, onThinking, onResume, onStarted, toast }: Props) {
  const input = useRef<HTMLInputElement>(null);
  const [drag, setDrag] = useState(false);
  const [progress, setProgress] = useState<{ loaded: number; total: number } | null>(null);
  const abort = useRef<(() => void) | null>(null);
  const uploading = progress !== null;

  const pick = (picked: File | undefined) => {
    if (!picked) return;
    if (!/\.pdf$/i.test(picked.name) && picked.type !== 'application/pdf') return toast('只支持 PDF 文件');
    if (picked.size > MAX_BYTES) return toast('单篇不超过 50 MB');
    onFile(picked);
  };

  const start = async () => {
    if (!file) return;
    setProgress({ loaded: 0, total: file.size });
    const task = upload(file, { model: MODELS[0], thinking }, (loaded, total) => setProgress({ loaded, total }));
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
      {running && (
        <div className="running">
          <span className="dot" />
          <div className="t">
            <b>{running.filename}</b> <span>{running.status === 'queued' ? '排队中' : '正在翻译'}</span>
          </div>
          <button className="btn btn-secondary sm" onClick={() => onResume(running.id)}>
            查看进度
          </button>
        </div>
      )}
      <div className="upload-card">
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
            <span className="t1">把 PDF 拖到这里</span>
            <button className="btn btn-secondary" onClick={() => input.current?.click()}>
              选择文件
            </button>
            <span className="t2">仅支持文字版 PDF · 单篇不超过 50 MB · 不超过 60 页</span>
          </div>
        ) : (
          <div className="picked">
            <div className="file-row">
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
      <div className={`options${uploading ? ' locked' : ''}`}>
        <div className="opt">
          <label>模型</label>
          <button className="select" title="目前仅此一个模型">
            {MODELS[0]}
            <Icon id="down" />
          </button>
        </div>
        <div className="opt">
          <label>思考强度</label>
          <div className="seg">
            {THINKING.map((level) => (
              <button key={level} className={level === thinking ? 'on' : ''} onClick={() => onThinking(level)}>
                {level}
              </button>
            ))}
          </div>
          <div className="note">越高译文越细致，耗时也越长</div>
        </div>
      </div>
      {file && !uploading && (
        <button className="btn btn-primary start" onClick={start}>
          开始翻译
        </button>
      )}
    </main>
  );
}
