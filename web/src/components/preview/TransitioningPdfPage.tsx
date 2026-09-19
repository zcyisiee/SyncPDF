import { useState } from 'react';
import { PdfCanvas, type PdfCanvasProps } from './PdfCanvas';

interface Source { url: string; pageNumber: number }
const identity = (source: Source) => `${source.url}#${source.pageNumber}`;

export function TransitioningPdfPage(props: PdfCanvasProps) {
  const requested = identity(props);
  const [current, setCurrent] = useState<Source | null>(null);
  const [ready, setReady] = useState<string | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const currentKey = current ? identity(current) : null;
  const replacing = currentKey !== requested;
  const reduced = typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const commit = () => { setCurrent({ url: props.url, pageNumber: props.pageNumber }); setReady(null); };
  const sources: Source[] = current ? [current] : [];
  if (replacing && failed !== requested) sources.push({ url: props.url, pageNumber: props.pageNumber });
  return <div className="relative h-full w-full">
    {sources.map((source) => {
      const key = identity(source);
      const incoming = key !== currentKey;
      const revealing = incoming && ready === key;
      return <div key={`${key}#${incoming ? attempt : 0}`} data-preview-layer={incoming ? 'incoming' : 'current'}
        className={revealing && !reduced && current ? 'preview-page-reveal absolute inset-0' : 'absolute inset-0'}
        style={{ opacity: incoming && current && !revealing ? 0 : 1 }}
        onAnimationEnd={(event) => { if (event.target === event.currentTarget && incoming && key === requested) commit(); }}>
        <PdfCanvas {...props} {...source}
          onPage={incoming || !replacing ? props.onPage : undefined}
          onError={() => { if (incoming && current) setFailed(key); }}
          onRenderComplete={() => {
            if (!incoming || key !== requested) return;
            if (!current || reduced) commit(); else setReady(key);
          }} />
      </div>;
    })}
    {failed === requested && replacing ? <button type="button" className="absolute right-2 top-2 rounded bg-white px-2 py-1 text-xs"
      onClick={() => { setFailed(null); setAttempt((value) => value + 1); }}>页面更新失败，重试</button> : null}
  </div>;
}
