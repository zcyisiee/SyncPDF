/**
 * 顶栏右侧的任务控制（开始翻译 / 取消 / 重试）——旧版「进度视图顶部的开始卡 + 活动卡」
 * 在视图栏合并后收进这一条：
 *
 * - 无活动 job 且最近一次已成功/没有 job → 一个「开始翻译」主按钮：模型/思考强度/dual/
 *   词表/AI 审校全部取**设置屏**保存的默认偏好（`ieet.translation-default`），不再重复展示；
 * - 有活动 job → 状态徽标 + 「取消」（confirm 后 `POST /jobs/{jid}/cancel`）；
 * - 最近一次失败/取消/中断 → 徽标 + 「重试」（重试 = 用同参数**发一个新 job**，不自动重跑
 *   —— 后端明确不重跑收费调用）；`action=retranslate` 例外：它不能走 `POST /jobs`
 *   （服务端不接受裸 retranslate，候选必须绑定段落），重试入口在段落面板的「AI 重译」。
 *
 * 提交/取消失败在按钮下方弹一张可关闭的错误卡（顶栏放不下长文案）。
 */
import { useState } from 'react';

import type { DocumentDetail, JobRecord } from '../../api/types';
import { describeApiError } from '../../lib/api';
import { readTranslationDefaults, useHarnessSelection } from '../../lib/harnesses';
import { activeJob, defaultFromStage, jobCardMode, jobOutcomeMessage } from '../../lib/jobs';
import { stageLabel } from '../../lib/humanize';
import { useArtifacts, useCancelJobMutation, useCreateJobMutation, useProfiles } from '../../lib/queries';
import { Button } from '../ui/Button';
import { ErrorCard } from '../ui/ErrorCard';
import { StatusBadge } from '../ui/StatusBadge';
import { Tooltip } from '../ui/Tooltip';

/**
 * job 状态 → 徽标文案/色调。**必须覆盖终态**：失败/取消后这里还在显示，徽标若还写
 * 「运行中」就是骗人。
 */
function jobBadge(
  job: JobRecord,
): { label: string; tone: 'idle' | 'run' | 'err' | 'pass'; running: boolean } {
  switch (job.status) {
    case 'queued':
      return { label: '排队中', tone: 'idle', running: false };
    case 'running': {
      const stage = job.from_stage ? ` · ${stageLabel(job.from_stage)}` : '';
      return { label: `运行中${stage}`, tone: 'run', running: true };
    }
    case 'succeeded':
      return { label: '已完成', tone: 'pass', running: false };
    case 'canceled':
      return { label: '已取消', tone: 'idle', running: false };
    default:
      return { label: '失败', tone: 'err', running: false };
  }
}

export function JobControls({
  did,
  document,
  jobs,
}: {
  did: string;
  document: DocumentDetail;
  jobs: readonly JobRecord[] | undefined;
}) {
  const profilesQuery = useProfiles();
  const selection = useHarnessSelection(profilesQuery.data ?? []);
  const artifactsQuery = useArtifacts(did);
  const createJob = useCreateJobMutation(did);
  const cancelJob = useCancelJobMutation(did);

  const profiles = profilesQuery.data ?? [];
  const active = activeJob(jobs);
  const mode = jobCardMode(jobs);
  const latest = jobs?.[0] ?? null;
  const hasAnything = (artifactsQuery.data?.length ?? 0) > 0;
  // 「开始翻译」的禁用原因（tooltip；undefined = 可用）。
  const startBlockReason = !artifactsQuery.isSuccess
    ? '正在读取产物清单…'
    : !hasAnything
      ? '该文档还没有源 PDF 或任何产物，没有可跑的内容'
      : profilesQuery.isError
        ? '翻译配置读取失败：请到设置屏重试'
        : profiles.filter((item) => item.builtin && item.has_translator).length === 0
          ? '没有可用的内置翻译模型：请到设置屏重试读取'
          : undefined;

  const start = () => {
    // 模型/思考强度/dual/词表/审校都来自设置屏的默认偏好；起点阶段自动判断
    // （已有 parse 产物 → translate 续跑，否则从 parse 起步，见 `defaultFromStage`）。
    const defaults = readTranslationDefaults();
    createJob.mutate({
      action: 'run',
      from: defaultFromStage(document),
      pages: null,
      dual: defaults.dual,
      profile: selection.profile,
      thinking: selection.thinking,
      use_glossary: defaults.useGlossary,
      preview_workers: defaults.previewWorkers,
      ...(defaults.reviewer ? { reviewer_profile: selection.profile } : {}),
    });
  };

  const retry = () => {
    if (latest === null) return;
    if (latest.action === 'run') {
      createJob.mutate({
        action: 'run',
        from: latest.from_stage ?? undefined,
        pages: latest.pages ?? undefined,
        dual: latest.dual,
        profile: latest.profile,
        thinking: latest.thinking,
        ...(latest.reviewer_profile ? { reviewer_profile: latest.reviewer_profile } : {}),
        // 重试沿用上一个 job 的词表开关（记录里的 use_glossary 已经是“生效后的值”）。
        use_glossary: latest.use_glossary,
        // 预览并行度沿用记录里生效的值；老 job 没有该字段时用设置屏默认。
        preview_workers: latest.preview_workers ?? readTranslationDefaults().previewWorkers,
      });
      return;
    }
    if (latest.action === 'check') {
      // check 的 from/pages/dual 都由服务端固定，带上它们会被 422 forbidden_field 拒绝。
      createJob.mutate({
        action: 'check',
        profile: latest.profile,
        thinking: latest.thinking,
        dual: false,
        use_glossary: false,
      });
      return;
    }
    if (latest.action === 'compile') {
      // compile 不调用模型；重试针对当前草稿，scope 只复用用户上次请求的范围。
      createJob.mutate({
        action: 'compile',
        scope: latest.requested_scope === 'pages' ? 'pages' : 'full',
        dual: false,
        use_glossary: false,
      });
    }
  };

  const describe = createJob.error
    ? describeApiError(createJob.error)
    : cancelJob.error
      ? describeApiError(cancelJob.error)
      : null;
  // 「知道了」只关掉当前这张错误卡；下一次 mutate 失败是新的 error 对象，卡会再出现。
  const [ignoredError, setIgnoredError] = useState<unknown>(null);
  const rawError = createJob.error ?? cancelJob.error;
  const showError = rawError !== null && rawError !== undefined && rawError !== ignoredError;

  return (
    <div className="relative flex items-center gap-s2" data-od-id="job-controls">
      {mode === 'active' && active !== null ? (
        <>
          <span data-od-id="active-job-status" data-status={active.status}>
            <StatusBadge tone={jobBadge(active).tone} running={jobBadge(active).running}>
              {jobBadge(active).label}
            </StatusBadge>
          </span>
          <Button
            variant="danger"
            size="sm"
            data-od-id="cancel-job"
            disabled={cancelJob.isPending}
            onClick={() => {
              const confirmed = window.confirm(
                '取消这个任务？会终止整个进程组（含 translator 子进程），且不会自动重跑。',
              );
              if (confirmed) cancelJob.mutate(active.job_id);
            }}
          >
            {cancelJob.isPending ? '正在取消…' : '取消'}
          </Button>
        </>
      ) : mode === 'failed' && latest !== null ? (
        <>
          <Tooltip content={jobOutcomeMessage(latest) ?? ''}>
            <span data-od-id="active-job-status" data-status={latest.status}>
              <StatusBadge tone={jobBadge(latest).tone} running={false}>
                {jobBadge(latest).label}
              </StatusBadge>
            </span>
          </Tooltip>
          {latest.action === 'retranslate' ? (
            // 重译 job 的"重试"不能走 POST /jobs（服务端不接受裸 retranslate：候选要绑定段落），
            // 重试入口就在段落面板的「AI 重译」；这里只把话说明白，不造一个必然 422 的按钮。
            <span className="font-mono text-micro text-ink-4" data-od-id="retranslate-retry-hint">
              在段落面板重试（AI 重译）
            </span>
          ) : (
            <Button
              variant="primary"
              size="sm"
              data-od-id="retry-job"
              disabled={createJob.isPending}
              onClick={retry}
            >
              {createJob.isPending ? '正在提交…' : '重试'}
            </Button>
          )}
        </>
      ) : (
        <Tooltip content={startBlockReason ?? ''}>
          <Button
            variant="primary"
            size="sm"
            data-od-id="start-job-submit"
            disabled={startBlockReason !== undefined || createJob.isPending}
            onClick={start}
          >
            {createJob.isPending ? '正在提交…' : '开始翻译'}
          </Button>
        </Tooltip>
      )}
      {showError ? (
        // ErrorCard 自带 role="alert"，这里不再套一层
        <div
          data-od-id="job-controls-error"
          className="absolute right-0 top-full z-40 mt-s2 w-[360px]"
        >
          <ErrorCard title={describe?.title} message={describe?.message}>
            <Button size="sm" onClick={() => setIgnoredError(rawError)}>
              知道了
            </Button>
          </ErrorCard>
        </div>
      ) : null}
    </div>
  );
}
