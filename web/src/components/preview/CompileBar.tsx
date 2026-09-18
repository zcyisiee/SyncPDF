/**
 * 预览工具条下方的编译状态条（api.md §3.2）：
 *
 * | `compile.status` | 显示 |
 * |---|---|
 * | `running` | 「编译中…」+ 脉冲点 + 「编辑已禁用」 |
 * | `failed` | 错误条 + `error_code`（来自最近的 compile job，契约的 `compile` 字段不带它）+ 重试 |
 * | `stale` | 「草稿比当前 PDF 新（PDF 修订 r{n}，草稿 r{m}）」+ 手动编译 |
 * | `ok` 且不 stale | 「已更新到 r{n}」 |
 * | `none` 且没有产物 | 不渲染（下载按钮自己会解释「还没有可下载产物」） |
 *
 * 失败原因取自 `GET /documents/{did}/jobs` 里最近一条 `action=compile` 的 `error_code`/
 * `error_message`：`CompileStatus` 只有 status/revision/stale/artifact，错误码不在详情里，
 * 所以要么从 job 记录读到真的，要么不显示（**不编造**错误码）。
 */
import { describeApiError } from '../../lib/api';
import type { CompileSummary } from '../../lib/download';
import { useCompileBlockMutation, useJobs } from '../../lib/queries';
import { useUiStore } from '../../stores/ui';
import { Button } from '../ui/Button';
import { ErrorCard } from '../ui/ErrorCard';
import { Tooltip } from '../ui/Tooltip';

export interface CompileBarProps {
  did: string;
  compile: CompileSummary | null | undefined;
  /** 当前草稿 revision（手动编译的 `base_revision`）；拿不到草稿时 null。 */
  draftRevision: number | null;
  /** 活动 job 期间草稿只读（服务端 409 `document_busy`）。 */
  busyJobId?: string | null;
}

export function CompileBar({ did, compile, draftRevision, busyJobId = null }: CompileBarProps) {
  const jobsQuery = useJobs(did);
  const selectedId = useUiStore((state) => state.selectedParagraphId);
  const compileMutation = useCompileBlockMutation(did, selectedId ?? '');
  const selectedJob = jobsQuery.data?.find((job) => job.effective_scope === 'block' && job.paragraph_id === selectedId);
  const hasLocalJobs = jobsQuery.data?.some((job) => job.effective_scope === 'block') ?? false;
  const currentJob = selectedJob?.revision === draftRevision ? selectedJob : null;
  const status = hasLocalJobs
    ? currentJob?.status === 'failed' ? 'failed' : currentJob?.status === 'succeeded' ? 'ok' : 'none'
    : compile?.status ?? 'none';
  const revision = compile?.revision ?? 0;
  const stale = hasLocalJobs ? currentJob?.status !== 'succeeded' : compile?.stale === true;
  const latestCompileJob = hasLocalJobs ? currentJob : jobsQuery.data?.find((job) => job.action === 'compile') ?? null;
  const errorCode = latestCompileJob?.error_code ?? null;
  const errorMessage = latestCompileJob?.error_message ?? null;
  // react-query 无错时 `error` 是 **null**（不是 undefined）：两者都要当“没有错误”，
  // 否则会渲染出 `请求失败：null` 这种假错误条。
  const mutationError =
    compileMutation.error === null || compileMutation.error === undefined
      ? null
      : describeApiError(compileMutation.error);

  const canCompile = selectedId !== null && draftRevision !== null && busyJobId === null && !compileMutation.isPending;
  const compileButton = (label: string, odId: string) => (
    <Button
      data-od-id={odId}
      disabled={!canCompile}
      onClick={() => {
        if (draftRevision === null || selectedId === null) return;
        compileMutation.mutate(draftRevision);
      }}
    >
      {compileMutation.isPending ? '正在提交…' : label}
    </Button>
  );

  const localJob = latestCompileJob?.effective_scope === 'block' ? latestCompileJob : null;
  if (localJob && localJob.revision === draftRevision && ['queued', 'running', 'succeeded'].includes(localJob.status)) {
    return <div data-od-id="compile-bar" className="border-b border-hair bg-ivory px-s5 py-[5px] text-tiny">
      {localJob.status === 'succeeded'
        ? `块 ${localJob.paragraph_id} 已编译并更新预览（草稿 r${draftRevision}）`
        : `正在编译 ${localJob.paragraph_id}…`}
    </div>;
  }

  if (status === 'running') {
    return (
      <div
        data-od-id="compile-bar"
        data-compile-status="running"
        className="flex flex-none items-center gap-s2 border-b border-hair bg-run-soft px-s5 py-[5px] text-tiny text-run-ink"
      >
        <span aria-hidden="true" className="pulse-dot h-[6px] w-[6px] rounded-full bg-current" />
        编译中…（草稿已保存，服务端在隔离副本里跑 apply + build）
        <span className="ml-auto text-ink-4">编辑已禁用，编译结束后可继续改</span>
      </div>
    );
  }

  if (status === 'failed') {
    return (
      <div data-od-id="compile-bar" data-compile-status="failed" className="flex-none border-b border-hair bg-ivory p-s3">
        <ErrorCard
          data-od-id="compile-error"
          title="上次编译失败"
          message={errorMessage ?? '最近一次编译没有成功（详情只给编译状态，错误码来自 compile job 记录）。'}
          detail={errorCode === null ? undefined : `error_code: ${errorCode}`}
        >
          {compileButton('重试编译', 'compile-retry')}
          <span className="font-mono text-micro text-ink-4">
            仍是 r{revision}（上一版成功发布的 PDF 仍可下载）
          </span>
        </ErrorCard>
        {mutationError === null ? null : (
          <p data-od-id="compile-submit-error" className="mt-s2 font-mono text-micro text-err">
            {mutationError.title}：{mutationError.message}
          </p>
        )}
      </div>
    );
  }

  if (stale) {
    return (
      <div
        data-od-id="compile-bar"
        data-compile-status="stale"
        className="flex flex-none flex-wrap items-center gap-s2 border-b border-hair bg-run-soft px-s5 py-[5px] text-tiny text-run-ink"
      >
        <span aria-hidden="true" className="h-[6px] w-[6px] flex-none rounded-full bg-current" />
        草稿比当前 PDF 新（PDF 修订 r{revision}，草稿 r{draftRevision ?? '?'}）。
        {selectedId ? `只编译选中块 ${selectedId}；使用已保存的内容。` : '请先选中一个段落框。'}
        {busyJobId === null ? null : (
          <Tooltip content={`文档有活动 job ${busyJobId}：任务期间草稿只读`}>
            <span className="font-mono text-micro text-ink-4">（有任务在跑）</span>
          </Tooltip>
        )}
        <span className="ml-auto">{compileButton('编译选中块', 'compile-now')}</span>
        {mutationError === null ? null : (
          <span data-od-id="compile-submit-error" className="font-mono text-micro text-err">
            {mutationError.title}：{mutationError.message}
          </span>
        )}
      </div>
    );
  }

  if (status === 'ok') {
    return (
      <div
        data-od-id="compile-bar"
        data-compile-status="ok"
        className="flex flex-none items-center gap-s2 border-b border-hair bg-pass-soft px-s5 py-[5px] text-tiny text-pass-ink"
      >
        <span aria-hidden="true" className="h-[6px] w-[6px] flex-none rounded-full bg-current" />
        已更新到 r{revision}（与当前草稿一致）
        <span className="ml-auto text-ink-4">编译成功 ≠ 质量通过，见右侧质量徽标</span>
      </div>
    );
  }

  return null;
}
