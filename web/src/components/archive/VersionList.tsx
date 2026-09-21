/**
 * 归档 tab 里的版本列表（api.md §3.7）：新 → 旧、当前版本高亮、按版本下载任一历史版本。
 *
 * 服务端语义（前端**不重复判定**）：
 * - 清单是**新 → 旧**；`current_revision` = 当前可下载的那一版（`output/` 的最新发布），
 *   不是"清单最新行"（两者正常时相同，但真相只有一个）；
 * - 每条的质量徽标走 W10 的判定表（`needs_fix` 黄标），**下载不禁用**：质量只记录不门禁；
 * - 下载走 `…/versions/<r>/pdf`（`<a download>` + 服务端 inline 文件名 → 落盘名带 `r<r>`）；
 * - 从没编译成功过 → 「还没有全量编译版本」，不是错误。
 *
 * stale（草稿比当前版本新）由同一个 tab 的 `ArchiveSummary` 负责提示，这里不写第二遍。
 *
 * 轮询：编译未落定（running / stale）时按 2s 取一次，发布一落定就停（与详情端点同一口径）。
 */
import { compileUnsettled, type CompileSummary } from '../../lib/download';
import { humanizeUpdatedAt } from '../../lib/humanize';
import { DOCUMENT_LIVE_REFETCH_MS, useVersions } from '../../lib/queries';
import { versionRows, type VersionRow } from '../../lib/versions';
import { Button } from '../ui/Button';
import { Chip } from '../ui/Chip';
import { ErrorCard } from '../ui/ErrorCard';
import { StatusBadge } from '../ui/StatusBadge';

export interface VersionListProps {
  did: string;
  /** 详情里的 `compile`（只用来判断要不要轮询；版本事实全部来自 §3.7 的响应）。 */
  compile?: CompileSummary | null;
}

export function VersionList({ did, compile }: VersionListProps) {
  const live = compileUnsettled(compile);
  const versionsQuery = useVersions(did, {
    refetchMs: live ? DOCUMENT_LIVE_REFETCH_MS : 0,
  });
  const rows = versionRows(did, versionsQuery.data);

  return (
    <div data-od-id="archive-versions">
      {versionsQuery.isError ? (
        <ErrorCard
          data-od-id="archive-error"
          error={versionsQuery.error}
          title="读取版本清单失败"
        >
          <Button onClick={() => void versionsQuery.refetch()} disabled={versionsQuery.isFetching}>
            重试
          </Button>
        </ErrorCard>
      ) : null}

      {versionsQuery.isPending ? (
        <p className="text-tiny text-ink-4" data-od-id="archive-loading">
          正在读取版本清单…
        </p>
      ) : null}

      {versionsQuery.isSuccess && rows.length === 0 ? (
        <p className="text-tiny text-ink-4" data-od-id="archive-empty">
          还没有全量编译版本
        </p>
      ) : null}

      {rows.length > 0 ? (
        <ul className="flex flex-col gap-s2" data-od-id="archive-list">
          {rows.map((row) => (
            <VersionListItem key={row.revision} row={row} />
          ))}
        </ul>
      ) : null}
    </div>
  );
}

/** 一行版本：r 徽标 + 当前标记 + 触发原因 + 质量徽标 + 时间 + 大小 + 下载。 */
export function VersionListItem({ row }: { row: VersionRow }) {
  return (
    <li
      data-od-id="archive-row"
      data-revision={row.revision}
      data-current={row.current ? 'true' : 'false'}
      className={
        row.current
          ? 'flex flex-wrap items-center gap-s2 rounded border border-hair-2 bg-ivory px-s3 py-s2 shadow-ring'
          : 'flex flex-wrap items-center gap-s2 rounded border border-hair bg-ivory px-s3 py-s2'
      }
    >
      <span data-od-id="archive-row-revision">
        <Chip tone={row.current ? 'accent' : 'default'}>r{row.revision}</Chip>
      </span>
      {row.current ? (
        <span data-od-id="archive-row-current">
          <Chip tone="pass" title="compile.artifact 指向的就是这一版（output/ 的最新发布）">
            当前版本
          </Chip>
        </span>
      ) : null}
      <span data-od-id="archive-row-trigger">
        <Chip tone="default" title={row.triggerTitle}>
          {row.triggerLabel}
        </Chip>
      </span>
      <span data-od-id="archive-row-quality">
        <StatusBadge tone={row.quality.tone} title={row.quality.title}>
          {row.quality.label}
        </StatusBadge>
      </span>
      <span
        className="font-mono text-micro text-ink-4 [font-variant-numeric:tabular-nums]"
        data-od-id="archive-row-time"
        title={row.createdAt}
      >
        {humanizeUpdatedAt(row.createdAt)}
      </span>
      <span
        className="font-mono text-micro text-ink-4 [font-variant-numeric:tabular-nums]"
        data-od-id="archive-row-size"
      >
        {row.sizeLabel}
      </span>
      <span
        className="ml-auto font-mono text-micro text-ink-4"
        data-od-id="archive-row-artifact"
        title={`归档产物：${row.item.artifact_name}`}
      >
        {row.item.artifact_name}
      </span>
      <a
        data-od-id="archive-row-download"
        data-revision={row.revision}
        href={row.href}
        download={row.fileName}
        className="inline-flex h-6 items-center gap-[6px] rounded border border-hair bg-ivory px-2 text-tiny leading-none tracking-[0.02em] text-ink-2 transition-colors hover:border-hair-2 hover:bg-sand hover:text-ink"
      >
        下载 r{row.revision}
      </a>
    </li>
  );
}
