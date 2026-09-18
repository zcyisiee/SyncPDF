/**
 * 编译产物下载与质量徽标的**判定表**（api.md §3.2/§1.5）：纯函数，无 React。
 *
 * 三条不可妥协的规则（brief 红线）：
 * 1. **没有可下载产物就不许启用**：`compile.artifact === null` → disabled（不冒充成功）；
 * 2. **旧产物不许标成最新**：`compile.stale` / `status=failed` 时下载仍然可用（上一版 PDF
 *    确实还在、仍可下载），但徽标必须显式说明「比草稿旧」/「编译失败」，文案不得写「最新」；
 * 3. **质量不许冒充通过**：只有 `quality.pipeline_ok === true` 才绿；`check.verdict=needs_fix`
 *    一律黄标「检查未通过」；其余一律中性表述（含 `not_available`）。
 */
import type { DocumentDetail } from '../api/types';
import type { StatusTone } from './humanize';

/** 只读的编译状态子集：字段缺失（老后端/测试替身）按「没有编译过」处理，不猜。 */
export interface CompileSummary {
  status?: string | null;
  revision?: number | null;
  stale?: boolean | null;
  artifact?: { name?: string | null; revision?: number | null; size?: number | null } | null;
}

/** 只读的质量状态子集。 */
export interface QualitySummary {
  check?: { verdict?: string | null } | null;
  reviewer?: { status?: string | null } | null;
  pipeline_ok?: boolean | null;
}

export interface DownloadState {
  /** 有可下载产物（`compile.artifact` 且名字可用）才 true。 */
  enabled: boolean;
  /** 服务端下载键：`output/<artifact.name>`（§3.2：artifact.name 是裸文件名）。 */
  artifactKey: string;
  /** 前端改名后的落盘文件名（如 `paper.mono.r7.pdf`），不满足形状时退回原名。 */
  fileName: string;
  /** 上一版可下载 PDF 的修订号（`artifact.revision`）；没有 → null。 */
  revision: number | null;
  /** 产物比草稿旧（或编译失败 → 旧产物仍在）→ 下载按钮旁必须显式提示。 */
  outdated: boolean;
  /** 质量门禁是否全绿（`pipeline_ok`）：只影响说明文案，不禁用下载。 */
  qualityOk: boolean;
  /** 禁用原因（tooltip）。 */
  disabledReason: string | null;
  /** 产物本身的状态徽标（`none` 之外都有值）。 */
  artifactBadge: { tone: StatusTone; label: string; title: string } | null;
}

function numberOf(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

/**
 * `paper.mono.pdf` + r7 → `paper.mono.r7.pdf`（**只改前端落盘名**，服务端产物名不动）。
 * 非 `.pdf` 或没有 revision 时原样返回（不乱造扩展名）。
 */
export function versionedFileName(name: string, revision: number | null): string {
  if (revision === null) return name;
  const suffix = `.r${revision}`;
  const dot = name.lastIndexOf('.');
  if (dot <= 0 || name.slice(dot).toLowerCase() !== '.pdf') return `${name}${suffix}`;
  // 保留原扩展名大小写（`paper.PDF` → `paper.r2.PDF`）
  return `${name.slice(0, dot)}${suffix}${name.slice(dot)}`;
}

/** 编译产物 + 质量 → 下载按钮状态（状态矩阵见 `tests/download-button.test.tsx`）。 */
export function downloadState(
  compile: CompileSummary | null | undefined,
  quality: QualitySummary | null | undefined,
): DownloadState {
  const artifact = compile?.artifact ?? null;
  const name = typeof artifact?.name === 'string' && artifact.name !== '' ? artifact.name : null;
  const revision = numberOf(artifact?.revision);
  const stale = compile?.stale === true;
  const failed = compile?.status === 'failed';
  const qualityOk = quality?.pipeline_ok === true;

  if (name === null) {
    return {
      enabled: false,
      artifactKey: '',
      fileName: '',
      revision: null,
      outdated: false,
      qualityOk,
      disabledReason:
        compile?.status === 'running'
          ? '编译进行中：完成后才能下载产物'
          : '还没有可下载的编译产物（先保存草稿或手动编译）',
      artifactBadge: null,
    };
  }

  const artifactBadge = failed
    ? {
        tone: 'err' as const,
        label: `编译失败 · 仍是 r${revision ?? 0}`,
        title: '最近一次编译失败：这里给的是**上一版**成功发布的 PDF（不是这次尝试的结果）',
      }
    : stale
      ? {
          tone: 'run' as const,
          label: `比草稿旧 · r${revision ?? 0}`,
          title: '草稿比这份 PDF 新：下载的是上一次成功发布的修订，不是最新草稿',
        }
      : {
          tone: 'pass' as const,
          label: `最新 · r${revision ?? 0}`,
          title: qualityOk
            ? '这份 PDF 与当前草稿一致，且质量门禁全绿（pipeline_ok=true）'
            : '这份 PDF 与当前草稿一致，但质量门禁未通过：见右侧质量徽标（编译成功 ≠ 质量通过）',
        };

  return {
    enabled: true,
    artifactKey: `output/${name}`,
    fileName: versionedFileName(name, revision),
    revision,
    outdated: stale || failed,
    qualityOk,
    disabledReason: null,
    artifactBadge,
  };
}

/**
 * 质量徽标（工具条右侧）：**只有 `pipeline_ok` 才绿**（编译成功 ≠ 质量通过）。
 * `needs_fix`（check 或 reviewer）显式黄标「检查未通过」。
 */
export function qualityBadge(quality: QualitySummary | null | undefined): {
  tone: StatusTone;
  label: string;
  title: string;
} {
  const checkVerdict = quality?.check?.verdict ?? null;
  const reviewerStatus = quality?.reviewer?.status ?? null;
  if (quality?.pipeline_ok === true) {
    return { tone: 'pass', label: '检查通过', title: 'quality.pipeline_ok=true：质量门禁全绿' };
  }
  if (checkVerdict === 'needs_fix' || reviewerStatus === 'needs_fix') {
    return {
      tone: 'run',
      label: '检查未通过',
      title: `check.verdict=${checkVerdict ?? '—'} · reviewer=${reviewerStatus ?? '—'}（pipeline_ok=false）`,
    };
  }
  if (reviewerStatus === 'waiting_for_reviewer' || reviewerStatus === 'needs_human_review') {
    return {
      tone: 'run',
      label: '待人工审查',
      title: `reviewer.status=${reviewerStatus}（pipeline_ok=false：门禁还没放行）`,
    };
  }
  if (checkVerdict === 'pass') {
    return {
      tone: 'run',
      label: '检查过、门禁未过',
      title: `check.verdict=pass 但 pipeline_ok=false（reviewer=${reviewerStatus ?? '—'}）`,
    };
  }
  return {
    tone: 'idle',
    label: checkVerdict === null || checkVerdict === 'not_available' ? '检查不可用' : `检查 ${checkVerdict}`,
    title: '没有可用的质量门禁结论（产物缺失或还没跑过 check）',
  };
}

/** 详情 → 下载/质量 UI 需要的两组输入（缺失字段按「没有」处理）。 */
export function downloadInputs(detail: DocumentDetail | undefined): {
  compile: CompileSummary | null;
  quality: QualitySummary | null;
} {
  return {
    compile: detail?.compile ?? null,
    quality: detail?.quality ?? null,
  };
}

/**
 * 预览/下载 URL 的修订号查询参数。
 *
 * 同名产物（`output/<name>`）每次编译都会被**原地替换**，URL 不变就一模一样 ——
 * 浏览器/pdf.js 会拿缓存里的旧字节（预览看不到新修订，下载甚至可能把旧 PDF 起一个
 * `*.r7.pdf` 的名）。带一个 `?r=<artifact.revision>` 就能让两边都重新取字节；
 * 服务端忽略未知 query 参数，Range 行为不变。
 */
export function withArtifactRevision(url: string, revision: number | null): string {
  if (revision === null) return url;
  return `${url}${url.includes('?') ? '&' : '?'}r=${revision}`;
}

/**
 * 编译产物在 `GET /artifacts` 清单里的键（`output/<裸文件名>`），没有产物 → null。
 * 预览用它判断“清单里选中要显示的那个 PDF 是不是编译产物”（是才需要按修订号刷新）。
 */
export function compileArtifactKey(compile: CompileSummary | null | undefined): string | null {
  const name = compile?.artifact?.name;
  return typeof name === 'string' && name !== '' ? `output/${name}` : null;
}

/** 编译产物的修订号（`artifact.revision`）；不是有限数字 → null。 */
export function compileArtifactRevision(compile: CompileSummary | null | undefined): number | null {
  return numberOf(compile?.artifact?.revision);
}

/**
 * 编译**还没落定**（正在编译，或草稿比已发布的 PDF 新）→ 详情值得按 2s 轮询：
 * 这两状态下 `compile` 会自己变（running → ok/failed，stale → false），不轮询就看不到。
 */
export function compileUnsettled(compile: CompileSummary | null | undefined): boolean {
  return compile?.status === 'running' || compile?.stale === true;
}
