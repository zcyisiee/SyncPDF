/**
 * 预览工具条右侧的下载按钮 + 质量徽标（api.md §1.5 / §3.2）。
 *
 * 三条红线（判定表在 `lib/download.ts`，这里只渲染）：
 * - 只有 `compile.artifact` 存在才启用（没有可用产物 = 禁用 + 说明原因，不冒充成功）；
 * - 旧产物（stale）与编译失败（failed）**仍可下载**，但徽标显式写「比草稿旧」/「编译失败 ·
 *   仍是 r{n}」——客户端落盘名带 `r{artifact.revision}` 后缀（仅前端改名，服务端产物名不动）；
 * - 质量徽标只有 `quality.pipeline_ok` 才绿，`needs_fix` 一律黄标「检查未通过」。
 *
 * W12 只加一个入口：这条链接跳到归档视图（`#/d/:did/archive`，§3.7）下载任一历史版本。
 * 主下载仍走 `output/<artifact.name>`（服务端发布的语义一个字不改）。
 */
import { artifactUrl } from '../../lib/preview';
import {
  downloadState,
  qualityBadge,
  withArtifactRevision,
  type CompileSummary,
  type QualitySummary,
} from '../../lib/download';
import { archiveHash } from '../../lib/versions';
import { cn } from '../../lib/cn';
import { StatusBadge } from '../ui/StatusBadge';
import { Tooltip } from '../ui/Tooltip';

export interface DownloadButtonProps {
  did: string;
  compile: CompileSummary | null | undefined;
  quality: QualitySummary | null | undefined;
}

export function DownloadButton({ did, compile, quality }: DownloadButtonProps) {
  const state = downloadState(compile, quality);
  const badge = qualityBadge(quality);
  const artifactBadge = state.artifactBadge;

  return (
    <div className="flex items-center gap-s2" data-od-id="download-group">
      {artifactBadge === null ? null : (
        <span data-od-id="compile-revision-badge">
          <StatusBadge tone={artifactBadge.tone} title={artifactBadge.title}>
            {artifactBadge.label}
          </StatusBadge>
        </span>
      )}
      <span data-od-id="quality-badge">
        <StatusBadge tone={badge.tone} title={badge.title}>
          {badge.label}
        </StatusBadge>
      </span>
      {state.enabled ? (
        <a
          data-od-id="download-button"
          data-enabled="true"
          data-file-name={state.fileName}
          // 同源 URL + `download` 属性 = 浏览器直接改名落盘（不把 65MB 级产物读进 JS 内存）；
          // 带 `?r=` 保证下载的字节就是徽标上那个修订（同名产物会被编译原地替换）。
          href={withArtifactRevision(artifactUrl(did, state.artifactKey), state.revision)}
          download={state.fileName}
          className={cn(
            'inline-flex h-6 items-center gap-[6px] rounded border border-hair bg-ivory px-2 text-tiny',
            'leading-none tracking-[0.02em] text-ink-2 transition-colors',
            'hover:border-hair-2 hover:bg-sand hover:text-ink',
          )}
        >
          下载 PDF
          {state.outdated ? <span className="font-mono text-micro text-run-ink">（旧版）</span> : null}
        </a>
      ) : (
        <Tooltip content={state.disabledReason ?? '没有可下载的产物'}>
          <span
            data-od-id="download-button"
            data-enabled="false"
            aria-disabled="true"
            className="inline-flex h-6 cursor-not-allowed items-center rounded border border-hair bg-ivory px-2 text-tiny leading-none tracking-[0.02em] text-ink-4 opacity-45"
          >
            下载 PDF
          </span>
        </Tooltip>
      )}
      <a
        data-od-id="download-history"
        href={archiveHash(did)}
        title="查看版本归档：任一历史版本都能下载（质量状态随版本记录）"
        className="text-tiny leading-none tracking-[0.02em] text-ink-3 underline decoration-hair-2 underline-offset-2 hover:text-ink"
      >
        历史版本
      </a>
    </div>
  );
}
