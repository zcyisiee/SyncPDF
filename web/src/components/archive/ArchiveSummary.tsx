/**
 * 右侧面板「归档」tab 的版本归档**简单摘要**：条数、最新一版（r 徽标 + 触发原因 + 质量徽标
 * + 时间 + 大小）、stale 提示，以及一条 `…/versions/<r>/pdf` 的直达下载。
 *
 * 只做摘要，不重复列表：同 tab 的 `VersionList` 给完整清单（含每一版）。
 * 数据与版本列表**同一个 query key**（`useVersions`），同屏两份消费也只打一次请求。
 * 自己不滚动、不控 padding：滚动与内边距由归档 tab 容器给（避免嵌套滚动区）。
 */
import { humanizeUpdatedAt } from '../../lib/humanize';
import { useVersions } from '../../lib/queries';
import { archiveHash, archiveSummary, versionPdfUrl } from '../../lib/versions';
import { Button, LinkButton } from '../ui/Button';
import { Chip } from '../ui/Chip';
import { ErrorCard } from '../ui/ErrorCard';
import { StatusBadge } from '../ui/StatusBadge';

export function ArchiveSummary({ did }: { did: string }) {
  const versionsQuery = useVersions(did);
  const summary = archiveSummary(did, versionsQuery.data);

  return (
    <div className="flex flex-col" data-od-id="archive-summary">
      <header className="flex items-baseline gap-s2">
        <p className="font-serif text-md font-medium leading-[1.4] text-ink-2">版本归档</p>
        <span className="ml-auto font-mono text-micro text-ink-4" data-od-id="archive-summary-count">
          {versionsQuery.isSuccess ? `${summary.count} 个版本` : '载入中…'}
        </span>
      </header>

      {versionsQuery.isError ? (
        <ErrorCard
          className="mt-s3"
          data-od-id="archive-summary-error"
          error={versionsQuery.error}
          title="读取版本清单失败"
        >
          <Button onClick={() => void versionsQuery.refetch()} disabled={versionsQuery.isFetching}>
            重试
          </Button>
        </ErrorCard>
      ) : null}

      {summary.empty && versionsQuery.isSuccess ? (
        <p className="mt-s3 text-tiny text-ink-4" data-od-id="archive-summary-empty">
          还没有版本：编译成功后自动归档一版（保留最近 50 个）。
        </p>
      ) : null}

      {summary.latest === null ? null : (
        <section className="mt-s3 flex flex-col gap-s2" data-od-id="archive-summary-latest">
          <span className="flex flex-wrap items-center gap-s2">
            <Chip tone="accent">r{summary.latest.revision}</Chip>
            <Chip tone="default" title={summary.latest.triggerTitle}>
              {summary.latest.triggerLabel}
            </Chip>
            <StatusBadge
              tone={summary.latest.quality.tone}
              title={summary.latest.quality.title}
            >
              {summary.latest.quality.label}
            </StatusBadge>
          </span>
          <span
            className="font-mono text-micro text-ink-4 [font-variant-numeric:tabular-nums]"
            title={summary.latest.createdAt}
          >
            {humanizeUpdatedAt(summary.latest.createdAt)} · {summary.latest.sizeLabel}
          </span>
          <a
            data-od-id="archive-summary-download"
            href={versionPdfUrl(did, summary.latest.revision)}
            download={summary.latest.fileName}
            className="inline-flex h-6 w-fit items-center rounded border border-hair bg-ivory px-2 text-tiny leading-none tracking-[0.02em] text-ink-2 transition-colors hover:border-hair-2 hover:bg-sand hover:text-ink"
          >
            下载 r{summary.latest.revision}
          </a>
        </section>
      )}

      {summary.stale ? (
        <p className="mt-s3 text-tiny text-run-ink" data-od-id="archive-summary-stale">
          草稿有未编译修改（当前可下载的是 r{summary.currentRevision}）
        </p>
      ) : null}

      <div className="mt-s4" data-od-id="archive-summary-all">
        <LinkButton href={archiveHash(did)} size="sm">
          查看全部
        </LinkButton>
      </div>
    </div>
  );
}
