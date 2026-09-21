/**
 * 文档操作区（`data-od-id="action-bar"`）：左「任务控制」（`JobControls`：开始翻译 / 取消 /
 * 重试 + 状态徽标），右「编译全文」。
 *
 * 它住**左栏论文导航的最底部**（`PaperNav` 的 `ScrollArea` 之后、全局导航之前），因为任务
 * 控制的唯一入口要在所有视图下常驻，而左栏在所有路由都常驻。只在工作台路由
 * （`PaperNav` 收到 `activeDid !== null`）挂载：文件库/词表/设置屏没有"当前文档"，
 * 那时显示「开始翻译」会指向一个不存在的对象。
 *
 * 迁移（右栏 `InspectorPanel` 底部 → 左栏）只换了宿主；`JobControls` 与
 * `CompileFullButton` 的行为、禁用条件、tooltip 文案保持原样。
 */
import type { JobRecord } from '../../api/types';
import { describeApiError } from '../../lib/api';
import { activeJob } from '../../lib/jobs';
import { useCompileDraftMutation, useDocument, useDraft, useJobs } from '../../lib/queries';
import { JobControls } from '../jobs/JobControls';
import { Button } from '../ui/Button';

export function DocumentActionBar({ did }: { did: string }) {
  const documentQuery = useDocument(did);
  const jobsQuery = useJobs(did);
  return (
    <div
      data-od-id="action-bar"
      className="flex flex-none flex-wrap items-center gap-s2 border-t border-hair bg-ivory px-s4 py-s2"
    >
      {documentQuery.data === undefined ? (
        // `JobControls` 要完整 `DocumentDetail`（起点阶段等）：详情没到就给占位，
        // 不拿半个对象硬渲染。
        <span className="text-tiny text-ink-4">读取文档…</span>
      ) : (
        <JobControls did={did} document={documentQuery.data} jobs={jobsQuery.data} />
      )}
      <span className="ml-auto flex items-center gap-s2">
        <CompileFullButton did={did} jobs={jobsQuery.data} />
      </span>
    </div>
  );
}

/**
 * 操作区右侧的「编译全文」：按**当前草稿 revision** 发 `action=compile & scope=full`
 * （`useCompileDraftMutation`），不碰段落文本、不调翻译模型。
 *
 * 禁用三态：草稿没加载完（拿不到 `base_revision`）/ 该文档有活动 job（服务端会 409
 * `document_busy`，这里提前挡住）/ 本次提交还在飞。逐块编译仍在段落 tab（`CompileBar`）。
 */
function CompileFullButton({
  did,
  jobs,
}: {
  did: string;
  jobs: readonly JobRecord[] | undefined;
}) {
  const draftQuery = useDraft(did);
  const compileFull = useCompileDraftMutation(did);
  const revision = draftQuery.data?.revision;
  const disabled = revision === undefined || activeJob(jobs) !== null || compileFull.isPending;
  // react-query 无错时 `error` 是 **null**（不是 undefined）：两者都当「没有错误」。
  const error =
    compileFull.error === null || compileFull.error === undefined
      ? null
      : describeApiError(compileFull.error);

  return (
    <>
      {error === null ? null : (
        <span data-od-id="compile-full-error" className="font-mono text-micro text-err">
          {error.title}：{error.message}
        </span>
      )}
      <Button
        size="sm"
        data-od-id="compile-full"
        disabled={disabled}
        title="基于当前草稿全文重排版（scope=full）；逐块编译在段落 tab"
        onClick={() => {
          if (revision === undefined) return;
          compileFull.mutate(revision);
        }}
      >
        {compileFull.isPending ? '正在提交…' : '编译全文'}
      </Button>
    </>
  );
}
