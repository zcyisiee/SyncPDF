/**
 * 版本归档（api.md §3.7）的**判定表**：纯函数，无 React。
 *
 * 三条口径（与 W10 的 `lib/download.ts` 同一思路）：
 * 1. **当前版本 = `compile.artifact` 那一版**（服务端的 `current_revision`），不是清单里的最新行
 *    —— 两者在正常情况下相同，但真相只有一个：`output/` 的最新发布；
 * 2. **质量徽标复用 W10**：版本快照只有 `{check_verdict, pipeline_ok}`，映射成 `qualityBadge`
 *    的输入形状后走同一份判定表（needs_fix 一律黄标，**不禁用下载**：只记录不门禁）；
 * 3. **下载 URL** = `…/versions/<r>/pdf`（服务端给 `inline` 文件名，前端 `<a download>` 决定落盘名）。
 */
import type { VersionItem, VersionsResponse } from '../api/types';
import { API_BASE } from './api';
import { qualityBadge, versionedFileName, type QualitySummary } from './download';
import type { StatusTone } from './humanize';
import { formatBytes } from './uploads';

/** 一个版本在归档视图里的一行（渲染所需的全部事实都在这里）。 */
export interface VersionRow {
  revision: number;
  /** 归档时刻（UTC ISO8601），显示用相对时间 + 绝对时间的 tooltip。 */
  createdAt: string;
  /** 服务端的 `current_revision` 就是这一版（当前可下载的那一份）。 */
  current: boolean;
  /** `debounce` | `manual`（原值，来自服务端）。 */
  trigger: string;
  triggerLabel: string;
  triggerTitle: string;
  quality: { tone: StatusTone; label: string; title: string };
  sizeLabel: string;
  /** 下载地址（`…/versions/<r>/pdf`）。 */
  href: string;
  /** 落盘名（`<原产物名去 .pdf>.r<r>.pdf`），与 W10 的下载改名同一形状。 */
  fileName: string;
  /** 原始清单行（详情/调试用）。 */
  item: VersionItem;
}

/** 触发原因 → 徽标文案（取不到/不认识的值原样显示，不猜）。 */
export function triggerLabel(trigger: string | null | undefined): {
  text: string;
  title: string;
} {
  if (trigger === 'debounce') {
    return { text: '自动', title: '草稿保存后服务端 1.5s 防抖自动编译（trigger=debounce）' };
  }
  if (trigger === 'manual') {
    return { text: '手动', title: '显式 POST compile 触发（trigger=manual）' };
  }
  return { text: trigger ?? '—', title: '服务端没有给出触发原因' };
}

/**
 * 版本里的质量快照 → 徽标（**复用 W10 的 `qualityBadge` 判定表**）。
 * 版本快照没有 reviewer 结论，只有 check 门禁 + `pipeline_ok`，映射时保持两者同源。
 */
export function versionQualityBadge(quality: VersionItem['quality']): {
  tone: StatusTone;
  label: string;
  title: string;
} {
  const summary: QualitySummary = {
    check: { verdict: quality.check_verdict },
    pipeline_ok: quality.pipeline_ok,
  };
  return qualityBadge(summary);
}

/** 某一版的下载地址（`GET /documents/{did}/versions/{r}/pdf`）。 */
export function versionPdfUrl(did: string, revision: number): string {
  return `${API_BASE}/documents/${encodeURIComponent(did)}/versions/${revision}/pdf`;
}

/** 归档视图的入口 hash（W10 的下载按钮与右侧面板的「查看全部」都指向它）。 */
export function archiveHash(did: string): string {
  return `#/d/${encodeURIComponent(did)}/archive`;
}

/**
 * 清单 → 渲染行（保持服务端给的新 → 旧顺序；`current_revision` 标出当前版本）。
 * 拿不到清单（还没加载/请求失败）→ 空数组，由调用方给空态/错误态。
 */
export function versionRows(
  did: string,
  versions: VersionsResponse | undefined | null,
): VersionRow[] {
  const current = versions?.current_revision ?? 0;
  return (versions?.items ?? []).map((item) => {
    const trigger = triggerLabel(item.trigger);
    return {
      revision: item.revision,
      createdAt: item.created_at,
      current: item.revision === current,
      trigger: item.trigger,
      triggerLabel: trigger.text,
      triggerTitle: trigger.title,
      quality: versionQualityBadge(item.quality),
      sizeLabel: formatBytes(item.bytes),
      href: versionPdfUrl(did, item.revision),
      fileName: versionedFileName(item.artifact_name, item.revision),
      item,
    };
  });
}

/** 归档摘要（右侧面板的「归档」tab 用）：条数 + 最新一版 + 当前版本 + stale。 */
export function archiveSummary(
  did: string,
  versions: VersionsResponse | undefined | null,
): {
  count: number;
  currentRevision: number;
  stale: boolean;
  latest: VersionRow | null;
  empty: boolean;
} {
  const rows = versionRows(did, versions);
  return {
    count: rows.length,
    currentRevision: versions?.current_revision ?? 0,
    stale: versions?.stale === true,
    latest: rows[0] ?? null,
    empty: rows.length === 0,
  };
}
