import type { DocumentDetail, JobRecord } from '../../api/types';
import { describeApiError } from '../../lib/api';
import { jobOutcomeMessage } from '../../lib/jobs';
import { useCancelJobMutation, useCreateJobMutation } from '../../lib/queries';
import { progressLabel, stageLabel } from '../../lib/humanize';
import { Button } from '../ui/Button';
import { ErrorCard } from '../ui/ErrorCard';
import { StatusBadge } from '../ui/StatusBadge';

/**
 * job 状态 → 徽标文案/色调。**必须覆盖终态**：这个卡在"最近一次失败/取消"时也显示，
 * 那时徽标若还写「运行中」就是骗人（e2e 的 DOM 快照抓到过这个）。
 * `succeeded` 由工作台交回 StartJobCard（这里只是让 switch 全覆盖）。
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

/**
 * 活动/最近失败任务的卡（有活动 job 时替换 StartJobCard）：
 *
 * - `queued`/`running`：状态徽标 + 「取消」（confirm 后 `POST /jobs/{jid}/cancel`）；
 * - `failed`/`canceled`/`interrupted`：如实显示 `error_code`/`error_message` + 「重试」
 *   （重试 = 用同参数**发一个新 job**，不自动重跑 —— 后端明确不重跑收费调用）；
 *   `action=retranslate`（W11 重译候选）例外：它不能走 `POST /jobs`（服务端不接受裸
 *   retranslate，候选必须绑定段落），重试入口在段落面板的「AI 重译」，这里只给提示文案；
 * - `succeeded` 不在这里（那时该显示开始卡，用户可以再跑一次）；
 * - 运行中的 `action=run` 额外显示「已译段落 N/M」（W14）：数据源是**父级给的**文档详情
 *   （`translated_count`/`paragraph_count`，§3.1），本卡不发任何请求。**这两个数不诚实不了**：
 *   它们来自 `agent/translated.jsonl`（apply 阶段写出的产物），而翻译是**一次整篇子进程调用**、
 *   运行中不落任何逐段产物 —— 所以运行中 N 不跳动，只在套版落盘后跳变。文案因此带
 *   「套版后更新」，也不做跳动动画/假百分比。
 */
export function ActiveJobCard({
  did,
  job,
  document,
}: {
  did: string;
  job: JobRecord;
  /** 文档详情（工作台的 `useDocument` 已经拿到了）：只用于阶段进度那两个计数。 */
  document?: DocumentDetail;
}) {
  const cancelJob = useCancelJobMutation(did);
  const createJob = useCreateJobMutation(did);
  const showProgress = job.status === 'running' && job.action === 'run';
  const active = job.status === 'queued' || job.status === 'running';
  const badge = jobBadge(job);
  const describe = cancelJob.error
    ? describeApiError(cancelJob.error)
    : createJob.error
      ? describeApiError(createJob.error)
      : null;

  const retry = () =>
    createJob.mutate({
      action: job.action,
      from: job.from_stage ?? undefined,
      pages: job.pages ?? undefined,
      dual: job.dual,
      profile: job.profile,
      // 重试沿用上一个 job 的词表开关（记录里的 use_glossary 已经是“生效后的值”）；
      // action=check 之类在服务端会归一成 false，这里如实回传。
      use_glossary: job.use_glossary,
    });

  return (
    <div className="flex flex-col gap-s3 p-s4" data-od-id="active-job-card" data-job-id={job.job_id}>
      <div className="flex flex-wrap items-center gap-s3">
        <span data-od-id="active-job-status" data-status={job.status}>
          <StatusBadge tone={badge.tone} running={badge.running}>
            {badge.label}
          </StatusBadge>
        </span>
        <span className="font-mono text-micro text-ink-4">{job.job_id}</span>
        <span className="text-tiny text-ink-3">
          profile <span className="font-mono text-ink-2">{job.profile}</span>
          {job.pages ? ` · 第 ${job.pages} 页` : ''}
          {job.dual ? ' · dual' : ''}
        </span>
        {active ? (
          <Button
            variant="danger"
            data-od-id="cancel-job"
            disabled={cancelJob.isPending}
            onClick={() => {
              const confirmed = window.confirm(
                '取消这个任务？会终止整个进程组（含 translator 子进程），且不会自动重跑。',
              );
              if (confirmed) cancelJob.mutate(job.job_id);
            }}
          >
            {cancelJob.isPending ? '正在取消…' : '取消'}
          </Button>
        ) : job.action === 'retranslate' ? (
          // 重译 job 的"重试"不能走 POST /jobs（服务端不接受裸 retranslate：候选要绑定段落），
          // 重试入口就在段落面板的「AI 重译」；这里只把话说明白，不造一个必然 422 的按钮。
          <span className="font-mono text-micro text-ink-4" data-od-id="retranslate-retry-hint">
            在段落面板重试（AI 重译）
          </span>
        ) : (
          <Button
            variant="primary"
            data-od-id="retry-job"
            disabled={createJob.isPending}
            onClick={retry}
          >
            {createJob.isPending ? '正在提交…' : '重试'}
          </Button>
        )}
      </div>

      {active ? null : (
        <p className="text-tiny text-ink-2" data-od-id="active-job-outcome">
          {jobOutcomeMessage(job)}
          {job.error_code ? <span className="ml-2 font-mono text-micro text-ink-4">{job.error_code}</span> : null}
        </p>
      )}
      {showProgress ? (
        <p
          className="font-mono text-micro text-ink-4 [font-variant-numeric:tabular-nums]"
          data-od-id="active-job-progress"
          data-translated={String(document?.translated_count ?? '')}
          data-paragraphs={String(document?.paragraph_count ?? '')}
          title="已译段落数来自 agent/translated.jsonl（套版阶段写出）；翻译是整篇单次子进程调用，运行中这个数不变，套版完成后才跳变"
        >
          已译段落 {progressLabel(document?.translated_count, document?.paragraph_count)}（套版后更新）
        </p>
      ) : null}
      {describe === null ? null : (
        <ErrorCard data-od-id="active-job-error" title={describe.title} message={describe.message} />
      )}
    </div>
  );
}
