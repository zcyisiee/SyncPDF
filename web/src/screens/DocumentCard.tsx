import type { DocumentListItem } from '../api/types';
import { countLabel, humanizeUpdatedAt, STAGE_NAMES } from '../lib/humanize';
import { StageBadge } from '../components/ui/StatusBadge';

/** 文件库文档卡（§4.4：ivory + 1px hair + r6，hover 才加 lift 阴影）。点击进工作台进度视图。 */
export function DocumentCard({ doc }: { doc: DocumentListItem }) {
  const title = doc.title ?? doc.did;
  return (
    <a
      href={`#/d/${encodeURIComponent(doc.did)}/progress`}
      data-od-id="doc-card"
      data-did={doc.did}
      className="flex min-w-0 flex-col gap-s3 rounded-card border border-hair bg-ivory p-s4 transition-shadow hover:border-hair-2 hover:shadow-lift"
    >
      <div className="min-w-0">
        <h3 className="break-words font-serif text-md font-medium leading-[1.35] text-ink">
          {title}
        </h3>
        {doc.title === null || doc.title === undefined ? null : (
          <p className="mt-1 break-all font-mono text-micro text-ink-4">{doc.did}</p>
        )}
      </div>
      <p className="flex flex-wrap gap-x-s3 gap-y-[5px] font-mono text-micro text-ink-4 [font-variant-numeric:tabular-nums]">
        <span>{countLabel(doc.pages, '页')}</span>
        <span>{countLabel(doc.paragraph_count, '段')}</span>
        <span>已译 {countLabel(doc.translated_count, '段')}</span>
        <span>{humanizeUpdatedAt(doc.updated_at)}</span>
      </p>
      <div className="grid grid-cols-4 gap-[5px]">
        {STAGE_NAMES.map((stage) => (
          <StageBadge key={stage} stage={stage} status={doc.stage_summary[stage] ?? 'not_run'} />
        ))}
      </div>
    </a>
  );
}
