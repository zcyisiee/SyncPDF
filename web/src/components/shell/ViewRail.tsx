import type { ReactNode } from 'react';
import type { DocumentDetail } from '../../api/types';
import { cn } from '../../lib/cn';
import { countLabel, progressLabel } from '../../lib/humanize';
import { WORKBENCH_VIEWS, type WorkbenchView } from '../../lib/routing';
import { Chip } from '../ui/Chip';
import { DocumentStatusBadge } from '../ui/StatusBadge';
import { Icon } from '../icons';

/** §8.1 二级导航（220px）：文档头（标题 + 状态徽标 + 页/段数）+ 5 个视图项。 */
export function ViewRail({
  did,
  view,
  doc,
  live = false,
  jobControls,
}: {
  jobControls?: ReactNode;
  did: string;
  view: WorkbenchView;
  doc?: DocumentDetail;
  /** 时间线判定的「正在跑」（W06）→ 文档头徽标显示「翻译中」脉冲。 */
  live?: boolean;
}) {
  return (
    <nav
      aria-label="视图导航"
      data-od-id="view-rail"
      className="col-start-1 row-start-1 flex min-h-0 flex-col gap-s4 overflow-auto border-r border-hair px-[10px] py-s4"
    >
      <div className="px-[2px]">
        <h2 className="truncate font-serif text-h2 font-medium leading-[1.35] text-ink">
          {doc?.title ?? did}
        </h2>
        <div className="mt-s2 flex flex-wrap items-center gap-s2">
          <DocumentStatusBadge stageSummary={doc?.stage_summary} live={live} />
          <span className="font-mono text-micro text-ink-4 [font-variant-numeric:tabular-nums]">
            {countLabel(doc?.pages, '页')}
          </span>
          <span className="font-mono text-micro text-ink-4 [font-variant-numeric:tabular-nums]">
            {countLabel(doc?.paragraph_count, '段')}
          </span>
        </div>
      </div>
      <ul className="flex flex-col gap-[2px]">
        {WORKBENCH_VIEWS.map((item) => {
          const active = item.id === view;
          return (
            <li key={item.id}>
              <a
                href={`#/d/${encodeURIComponent(did)}/${item.id}`}
                data-view={item.id}
                aria-current={active ? 'page' : undefined}
                className={cn(
                  'flex h-8 items-center gap-s3 rounded px-s3 text-body transition-colors',
                  active
                    ? 'bg-ivory text-ink shadow-ring'
                    : 'text-ink-3 hover:bg-sand hover:text-ink',
                )}
              >
                <Icon name={item.icon} className={active ? 'text-accent' : 'text-ink-4'} />
                <span className="flex-1 text-left">{item.label}</span>
                {item.id === 'progress' && doc !== undefined ? (
                  <Chip title="已译段数 / 段落数">
                    {progressLabel(doc.translated_count, doc.paragraph_count)}
                  </Chip>
                ) : null}
              </a>
            </li>
          );
        })}
      </ul>
      {jobControls ? <div className="mt-s2 border-t border-hair pt-s3" data-od-id="job-panel-rail">{jobControls}</div> : null}
    </nav>
  );
}
