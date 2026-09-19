/**
 * 批量编译面板（shift 多选 ≥2 段时显示在右栏「段落」tab 顶部，api.md 的
 * `POST /documents/{did}/blocks/compile`）：列出已选块数与 id 摘要，一键把整组
 * 交给一个 job（同页串行、跨页并行，每个受影响页只合成一次）。
 *
 * 状态口径（参照 `CompileBar` 的单块版写法）：
 * - 在 `GET /jobs` 里找最近一条 `effective_scope='blocks'` 的 job，且只认
 *   `revision === 当前草稿 revision` 的那条（草稿又动过就不再冒充"这一批的结果"）；
 * - running（含 queued）→ 进行中提示（SSE 的 `block_compiled` 事件每块一条，近实时）；
 * - succeeded → 「已编译 N 块」（N 来自 job envelope 的 `blocks` 计数）；
 * - failed → 错误条展示 `error_message`（形如「N 个 block 编译失败（其余 M 个已发布）」，
 *   `GET /jobs/{jid}` 的 detail 不保证结构，这里只展示 message）。
 *
 * 提交错误（409 `revision_conflict` / 404 `block_not_found`）走 `describeApiError`，
 * 与单块编译同一套文案。
 */
import { describeApiError } from '../../lib/api';
import { useCompileBlocksMutation, useDraft, useJobs } from '../../lib/queries';
import { useUiStore } from '../../stores/ui';
import { Button } from '../ui/Button';
import { ErrorCard } from '../ui/ErrorCard';

/** id 摘要最多展示几个（其余折叠成 `…`，完整清单就是当前多选集合本身）。 */
const ID_PREVIEW_COUNT = 3;

export interface BatchPanelProps {
  did: string;
  /** 已选块 id（来自 ui store 的 `selectedParagraphIds`，顺序即选择顺序）。 */
  blockIds: string[];
  /** 只读态：编译进行中或该文档有活动 job（提交也会 409 `document_busy`）。 */
  disabled?: boolean;
  disabledReason?: string;
}

export function BatchPanel({ did, blockIds, disabled = false, disabledReason }: BatchPanelProps) {
  const clearParagraphSelection = useUiStore((state) => state.clearParagraphSelection);
  const draftQuery = useDraft(did);
  const jobsQuery = useJobs(did);
  const compileMutation = useCompileBlocksMutation(did);

  const draftRevision = draftQuery.data?.revision ?? null;
  // jobs 列表新 → 旧：第一条 blocks job 就是最近一批；再按 revision 过滤（CompileBar 同款）
  const blocksJob = jobsQuery.data?.find((job) => job.effective_scope === 'blocks') ?? null;
  const currentJob =
    blocksJob !== null && draftRevision !== null && blocksJob.revision === draftRevision
      ? blocksJob
      : null;

  // react-query 无错时 `error` 是 **null**（不是 undefined）：两者都当"没有错误"。
  const mutationError =
    compileMutation.error === null || compileMutation.error === undefined
      ? null
      : describeApiError(compileMutation.error);

  const idsPreview = blockIds.slice(0, ID_PREVIEW_COUNT).join(' ');

  return (
    <section
      data-od-id="batch-panel"
      className="flex-none border-b border-hair bg-sand px-s3 py-s2"
    >
      <p className="flex flex-wrap items-baseline gap-s2">
        <span className="text-tiny text-ink-2" data-od-id="batch-count">
          已选 {blockIds.length} 块
        </span>
        <span className="font-mono text-micro text-ink-4" data-od-id="batch-ids">
          {idsPreview}
          {blockIds.length > ID_PREVIEW_COUNT ? ' …' : ''}
        </span>
      </p>

      <div className="mt-2 flex flex-wrap items-center gap-s2">
        <Button
          data-od-id="batch-compile"
          disabled={
            disabled || blockIds.length === 0 || draftRevision === null || compileMutation.isPending
          }
          onClick={() => {
            if (draftRevision === null) return;
            compileMutation.mutate({ blockIds, baseRevision: draftRevision });
          }}
        >
          {compileMutation.isPending ? '正在提交…' : '批量编译'}
        </Button>
        <Button data-od-id="batch-cancel-select" variant="ghost" onClick={() => clearParagraphSelection()}>
          取消多选
        </Button>
        {disabled && disabledReason !== undefined ? (
          <span className="font-mono text-micro text-ink-4" data-od-id="batch-disabled-reason">
            {disabledReason}
          </span>
        ) : null}
      </div>

      {mutationError !== null ? (
        <p className="mt-s2 font-mono text-micro text-err" data-od-id="batch-submit-error">
          {mutationError.title}：{mutationError.message}
        </p>
      ) : null}

      {currentJob !== null ? <BatchJobStatus job={currentJob} count={blockIds.length} /> : null}
    </section>
  );
}

/** 该批 job 的三态展示（只处理与当前草稿 revision 匹配的那条，见上文口径）。 */
function BatchJobStatus({
  job,
  count,
}: {
  job: NonNullable<ReturnType<typeof useJobs>['data']>[number];
  count: number;
}) {
  if (job.status === 'queued' || job.status === 'running') {
    return (
      <p
        data-od-id="batch-job-running"
        className="mt-s2 flex items-center gap-s2 text-tiny text-run-ink"
      >
        <span aria-hidden="true" className="pulse-dot h-[6px] w-[6px] rounded-full bg-current" />
        批量编译中…（SSE block_compiled 事件可近实时）
      </p>
    );
  }
  if (job.status === 'succeeded') {
    const blocks = envelopeBlocks(job.envelope);
    return (
      <p data-od-id="batch-job-succeeded" className="mt-s2 text-tiny text-ink-3">
        {blocks === null ? `批量编译完成（共 ${count} 块）` : `已编译 ${blocks} 块`}
      </p>
    );
  }
  if (job.status === 'failed') {
    return (
      <ErrorCard
        className="mt-s2"
        data-od-id="batch-job-error"
        title="批量编译失败"
        message={job.error_message ?? '批量编译没有成功（详见 job 记录）。'}
      />
    );
  }
  // canceled / interrupted：job 没跑完不算失败，也不冒充成功——一句话说明即可。
  return (
    <p data-od-id="batch-job-stopped" className="mt-s2 text-tiny text-ink-4">
      该批编译已结束（{job.status}），可重新提交。
    </p>
  );
}

/** job envelope（JSON 字符串）里的 `blocks` 计数；缺/坏 → null（不编造数字）。 */
function envelopeBlocks(envelope: string | null | undefined): number | null {
  if (envelope === null || envelope === undefined) return null;
  try {
    const parsed = JSON.parse(envelope) as { blocks?: unknown };
    return typeof parsed.blocks === 'number' && Number.isFinite(parsed.blocks) ? parsed.blocks : null;
  } catch {
    return null;
  }
}
