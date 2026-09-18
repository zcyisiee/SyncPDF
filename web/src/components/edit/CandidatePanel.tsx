/**
 * AI 重译候选面板（api.md §3.6，W11）：挂在该段编辑器的下方。
 *
 * 契约要点（前端不许自作主张的部分）：
 * - **候选不是译文**：这里只调 `retranslate` / `adopt` / `reject`，绝不本地把候选写进正文或
 *   其它区域；采用后是**服务端**把候选写进草稿（返回新草稿），译文框变文本来自草稿缓存更新；
 * - **命令只来自 profile**：只传 profile **id**（请求体里没有 translator/feedback 字段）；
 * - **拒绝不动草稿**：因此拒绝按钮在活动 job 期间也**不**禁用（服务端不受忙限制），
 *   而「AI 重译」（排队）与「采用」（写草稿）会受 409 `document_busy`。
 *
 * 显示口径：每条候选给三段对比（原文 / 当前译文 / 候选译文）。「当前译文」取服务端当前
 * 生效值（草稿覆盖优先于 `translated.jsonl` 基线），所以它随采用/手改一起变。
 * 已决定的候选（已采用/已拒绝）折到 `<details>` 里，默认收起——列表只留还能操作的那条。
 */
import type { CandidateItem } from '../../api/types';
import { describeApiError } from '../../lib/api';
import {
  CANDIDATES_PENDING_REFETCH_MS,
  useAdoptCandidateMutation,
  useCandidates,
  useProfiles,
  useRejectCandidateMutation,
  useRetranslateMutation,
} from '../../lib/queries';
import { useUiStore } from '../../stores/ui';
import { Button } from '../ui/Button';
import { Chip } from '../ui/Chip';
import { ErrorCard } from '../ui/ErrorCard';
import { Tooltip } from '../ui/Tooltip';

export interface CandidatePanelProps {
  did: string;
  pid: string;
  /** 该段**当前生效**的译文（草稿覆盖优先；用来与候选做对比，不是本地面板状态）。 */
  currentTarget: string;
  /** 该段的译文基线（`GET /paragraphs` 的 `target`）；草稿没覆盖时与 `currentTarget` 同值。 */
  baselineTarget: string | null;
  /** 只读态（有活动 job / 编译中）：生成与采用禁用；拒绝不禁用（不写草稿）。 */
  disabled?: boolean;
  disabledReason?: string;
}

/** 候选状态 → 显示文案与色调（`pending` 又分"生成中"和"可操作"）。 */
function statusLabel(item: CandidateItem): { text: string; tone: 'default' | 'run' | 'pass' | 'err' } {
  if (item.status === 'adopted') return { text: '已采用', tone: 'pass' };
  if (item.status === 'rejected') return { text: '已拒绝', tone: 'err' };
  return item.candidate_target === null
    ? { text: '生成中', tone: 'run' }
    : { text: '待采用', tone: 'default' };
}

export function CandidatePanel({
  did,
  pid,
  currentTarget,
  baselineTarget,
  disabled = false,
  disabledReason,
}: CandidatePanelProps) {
  const profilesQuery = useProfiles();
  const candidatesQuery = useCandidates(did, pid);
  const retranslate = useRetranslateMutation(did, pid);
  const adopt = useAdoptCandidateMutation(did, pid);
  const reject = useRejectCandidateMutation(did, pid);
  const remembered = useUiStore((state) => state.retranslateProfile);
  const setRemembered = useUiStore((state) => state.setRetranslateProfile);

  // 只有配了 translator 的 profile 能生成候选（没配的服务端会 422 `profile_missing`）。
  const profiles = (profilesQuery.data ?? []).filter((item) => item.has_translator);
  // 「或用上次」：上次选过的 profile 还在列表里就继续用它，否则回第一个。
  const profile =
    remembered !== null && profiles.some((item) => item.id === remembered)
      ? remembered
      : (profiles[0]?.id ?? '');

  const items = candidatesQuery.data?.items ?? [];
  const queryError = candidatesQuery.isError || profilesQuery.isError;
  const open = items.filter((item) => item.status === 'pending');
  const decided = items.filter((item) => item.status !== 'pending');
  const generating = open.some((item) => item.candidate_target === null);
  const canGenerate = !disabled && profile !== '' && !retranslate.isPending;

  return (
    <section className="mt-s4 border-t border-hair pt-s3" data-od-id="candidate-panel" data-paragraph-id={pid}>
      <div className="flex flex-wrap items-center gap-s2">
        <span className="text-tiny text-ink-3">AI 重译候选</span>
        {generating ? (
          <Chip tone="run" title={`候选列表每 ${CANDIDATES_PENDING_REFETCH_MS / 1000}s 刷新一次`}>
            生成中…
          </Chip>
        ) : null}
        <span className="ml-auto font-mono text-micro text-ink-4" data-od-id="candidate-count">
          {open.length} 条待定
        </span>
      </div>

      {queryError ? <p className="mt-2 text-micro text-err">翻译配置或候选读取失败，请刷新后重试。</p> : null}
      <div className="mt-2 flex flex-wrap items-end gap-s2">
        <label className="flex min-w-0 flex-col gap-1">
          <span className="text-tiny text-ink-4">profile（命令由服务端解析）</span>
          <select
            data-od-id="retranslate-profile"
            aria-label="重译 profile"
            value={profile}
            disabled={disabled || profiles.length === 0}
            onChange={(event) => setRemembered(event.target.value)}
            className="h-6 rounded border border-hair bg-ivory px-1 font-mono text-micro text-ink-2 disabled:opacity-45"
          >
            {profiles.length === 0 ? <option value="">（没有可用 profile）</option> : null}
            {profiles.map((item) => (
              <option key={item.id} value={item.id}>
                {item.label}
              </option>
            ))}
          </select>
        </label>
        <Tooltip content={disabled ? (disabledReason ?? '当前不可提交') : '对这一段让 AI 重新翻译（不采用就不改译文）'}>
          <span>
            <Button
              data-od-id="retranslate-start"
              size="sm"
              disabled={!canGenerate}
              onClick={() => {
                if (profile === '') return;
                setRemembered(profile);
                retranslate.mutate(profile);
              }}
            >
              AI 重译
            </Button>
          </span>
        </Tooltip>
        {profilesQuery.isSuccess && profiles.length === 0 ? (
          <span className="text-micro text-ink-4" data-od-id="retranslate-no-profile">
            没有可用翻译配置：请先在翻译配置中创建模型，或展开高级脚本 profile。 <a className="underline" href="#/settings">管理翻译配置</a>
          </span>
        ) : null}
      </div>

      {open.map((item) => (
        <CandidateCard
          key={item.id}
          item={item}
          currentTarget={currentTarget}
          baselineTarget={baselineTarget}
          disabled={disabled}
          disabledReason={disabledReason}
          adoptPending={adopt.isPending}
          rejectPending={reject.isPending}
          onAdopt={() => adopt.mutate(item.id)}
          onReject={() => reject.mutate(item.id)}
        />
      ))}

      {decided.length > 0 ? (
        <details className="mt-s3" data-od-id="candidate-decided">
          <summary className="cursor-pointer text-tiny text-ink-4">
            已决定的候选（{decided.length}）
          </summary>
          <ul className="mt-2 flex flex-col gap-s2">
            {decided.map((item) => (
              <li key={item.id} className="flex items-start gap-s2" data-od-id="candidate-decided-row">
                <Chip tone={statusLabel(item).tone}>{statusLabel(item).text}</Chip>
                <span className="min-w-0 flex-1">
                  <span className="font-mono text-micro text-ink-4">
                    {item.id}
                    {item.model_label === null || item.model_label === undefined
                      ? ''
                      : ` · ${item.model_label}`}
                  </span>
                  <span className="mt-1 block max-h-[52px] overflow-hidden break-words font-serif text-tiny leading-[1.5] text-ink-3">
                    {item.candidate_target ?? '（没有译文）'}
                  </span>
                </span>
              </li>
            ))}
          </ul>
        </details>
      ) : null}

      {retranslate.error === null || retranslate.error === undefined ? null : (
        <ErrorCard
          className="mt-s3"
          data-od-id="retranslate-error"
          error={retranslate.error}
          title="提交重译失败"
        />
      )}
      {adopt.error === null || adopt.error === undefined ? null : (
        <ErrorCard
          className="mt-s3"
          data-od-id="adopt-error"
          error={adopt.error}
          title="采用候选失败"
          message={`${describeApiError(adopt.error).message}（候选仍是待定：采用失败不改草稿）`}
        />
      )}
      {reject.error === null || reject.error === undefined ? null : (
        <ErrorCard
          className="mt-s3"
          data-od-id="reject-error"
          error={reject.error}
          title="拒绝候选失败"
        />
      )}
    </section>
  );
}

function CandidateCard({
  item,
  currentTarget,
  baselineTarget,
  disabled,
  disabledReason,
  adoptPending,
  rejectPending,
  onAdopt,
  onReject,
}: {
  item: CandidateItem;
  currentTarget: string;
  baselineTarget: string | null;
  disabled: boolean;
  disabledReason?: string;
  adoptPending: boolean;
  rejectPending: boolean;
  onAdopt: () => void;
  onReject: () => void;
}) {
  const status = statusLabel(item);
  const ready = item.candidate_target !== null;

  return (
    <article
      className="mt-s3 rounded border border-hair bg-parchment p-s3"
      data-od-id="candidate-card"
      data-candidate-id={item.id}
      data-status={item.status}
      data-ready={ready ? 'true' : 'false'}
    >
      <header className="flex flex-wrap items-center gap-s2">
        <Chip tone={status.tone}>{status.text}</Chip>
        <span className="font-mono text-micro text-ink-4" data-od-id="candidate-model">
          {item.id}
          {item.model_label === null || item.model_label === undefined ? '' : ` · ${item.model_label}`}
        </span>
        <span className="ml-auto flex items-center gap-s2">
          <Button
            data-od-id="candidate-adopt"
            size="sm"
            variant="primary"
            disabled={disabled || !ready || adoptPending}
            title={disabled ? (disabledReason ?? '当前不可编辑') : '写入草稿并触发编译'}
            onClick={onAdopt}
          >
            采用
          </Button>
          <Button
            data-od-id="candidate-reject"
            size="sm"
            // 拒绝不改草稿 → 活动 job 期间也允许（服务端不受 document_busy 限制）
            disabled={rejectPending}
            onClick={onReject}
          >
            拒绝
          </Button>
        </span>
      </header>

      <dl className="mt-2 flex flex-col gap-[6px]">
        <CandidateRow label="原文" text={item.source ?? '（该段没有原文产物）'} />
        <CandidateRow
          label="当前译文"
          text={currentTarget}
          hint={baselineTarget !== null && baselineTarget !== currentTarget ? '（草稿版）' : undefined}
        />
        {ready ? (
          <CandidateRow label="候选译文" text={item.candidate_target ?? ''} accent />
        ) : (
          <div className="font-mono text-micro text-run-ink" data-od-id="candidate-generating">
            候选译文生成中（job 结束后出现在这里）
          </div>
        )}
      </dl>
    </article>
  );
}

function CandidateRow({
  label,
  text,
  hint,
  accent = false,
}: {
  label: string;
  text: string;
  hint?: string;
  accent?: boolean;
}) {
  return (
    <div>
      <dt className="font-mono text-micro text-ink-4">
        {label}
        {hint === undefined ? '' : ` ${hint}`}
      </dt>
      <dd
        className={`mt-[2px] max-h-[84px] overflow-auto whitespace-pre-wrap break-words rounded border border-hair bg-ivory p-2 font-serif text-tiny leading-[1.5] ${
          accent ? 'text-accent' : 'text-ink-3'
        }`}
      >
        {text}
      </dd>
    </div>
  );
}
