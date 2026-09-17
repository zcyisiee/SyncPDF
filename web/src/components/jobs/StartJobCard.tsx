import { useState } from 'react';

import type { DocumentDetail } from '../../api/types';
import { describeApiError } from '../../lib/api';
import {
  RUN_STAGES,
  defaultFromStage,
  isValidPagesSpec,
  normalizePagesSpec,
} from '../../lib/jobs';
import {
  useArtifacts,
  useCreateJobMutation,
  useGlossary,
  useProfiles,
} from '../../lib/queries';
import { stageLabel } from '../../lib/humanize';
import { Button } from '../ui/Button';
import { ErrorCard } from '../ui/ErrorCard';
import { Tooltip } from '../ui/Tooltip';

/** MinerU 的现实约束（写清楚"不是前端能决定的"）：token 只走 serve 进程环境。 */
const MINERU_NOTE =
  '从 parse 起步：需要 MinerU（token 由 serve 进程环境 MINERU_API_TOKEN 提供），耗时较长；已有解析产物时默认从 translate 续跑。';

/**
 * 「开始翻译」配置卡（进度视图顶部）：profile 下拉 + 页码 + dual + 词表开关 + 高级 from，
 * 提交 `POST /documents/{did}/jobs`。
 *
 * 只传**服务端接受的字段**（action/from/pages/dual/profile/use_glossary）：translator/reviewer
 * 命令由服务端从 profile 解析，前端连字段都没有（带了会被 422 `forbidden_field`）。
 *
 * 词表开关（W13，§3.8）：客户端只给 `use_glossary` 布尔（缺省 true），注入由服务端把
 * `<store_base>/.bdt-serve/glossary.csv` 的路径经 `--glossaries` 交给子进程。词表为空时
 * **禁用开关 + 提示「词表为空」**（开着也没用，服务端不会注入）；词表有内容时开关默认开着，
 * 用户可关掉这一次的注入。
 *
 * 显示条件（brief）：这个文档没有活动 job，且**有 source.pdf 或已有产物** —— 既没有源 PDF 也
 * 没有任何产物的空目录没有可跑的东西，不显示（不造假按钮）。
 */
export function StartJobCard({
  did,
  document,
}: {
  did: string;
  document: DocumentDetail | undefined;
}) {
  const artifactsQuery = useArtifacts(did);
  const profilesQuery = useProfiles();
  const glossaryQuery = useGlossary();
  const createJob = useCreateJobMutation(did);
  const artifacts = artifactsQuery.data ?? [];
  const profiles = profilesQuery.data ?? [];
  const glossaryCount = glossaryQuery.data?.count ?? 0;
  // 只有拿到服务端的答复才能说“词表为空”：请求失败/还没回来时不冒充空表，也不阻止提交
  // （注入终归由服务端判定）。
  const glossaryKnown = glossaryQuery.data !== undefined;
  const glossaryEmpty = glossaryKnown && glossaryCount === 0;
  // 只有"用户改过"才落到 state：默认值由数据推导（不在 effect 里写 state）。
  const [profileOverride, setProfileOverride] = useState<string | null>(null);
  const [fromOverride, setFromOverride] = useState<string | null>(null);
  const [pages, setPages] = useState('');
  const [dual, setDual] = useState(false);
  const [useGlossaryOverride, setUseGlossaryOverride] = useState<boolean | null>(null);
  const profile = profileOverride ?? profiles[0]?.id ?? '';
  const from = fromOverride ?? defaultFromStage(document);
  // 词表为空时开关恒为关（禁用）；有词表时默认开，用户可关。
  const glossaryOn = glossaryEmpty ? false : (useGlossaryOverride ?? true);

  const hasAnything = artifacts.length > 0;
  if (artifactsQuery.isPending || !hasAnything) return null;

  const pagesValid = isValidPagesSpec(pages);
  const canSubmit = profile !== '' && pagesValid && !createJob.isPending;
  const describe = createJob.error ? describeApiError(createJob.error) : null;

  return (
    <div className="flex flex-col gap-s3 p-s4" data-od-id="start-job-card">
      <div className="flex flex-wrap items-end gap-s3">
        <label className="flex min-w-0 flex-col gap-1">
          <span className="text-tiny text-ink-3">profile</span>
          <select
            data-od-id="start-job-profile"
            value={profile}
            onChange={(event) => setProfileOverride(event.target.value)}
            className="h-7 rounded border border-hair bg-ivory px-2 font-mono text-sm text-ink-2"
          >
            {profiles.length === 0 ? <option value="">（没有可用 profile）</option> : null}
            {profiles.map((item) => (
              <option key={item.id} value={item.id}>
                {item.label}
                {item.has_translator ? '' : '（无 translator）'}
              </option>
            ))}
          </select>
        </label>

        <label className="flex min-w-0 flex-col gap-1">
          <span className="text-tiny text-ink-3">页码范围（留空 = 全部）</span>
          <input
            data-od-id="start-job-pages"
            value={pages}
            onChange={(event) => setPages(event.target.value)}
            placeholder="1-3,5 全部留空"
            className="h-7 w-[150px] rounded border border-hair bg-ivory px-2 font-mono text-sm text-ink-2"
          />
        </label>

        <label className="flex h-7 items-center gap-2 text-tiny text-ink-3">
          <input
            type="checkbox"
            data-od-id="start-job-dual"
            checked={dual}
            onChange={(event) => setDual(event.target.checked)}
          />
          生成 dual（双语 PDF）
        </label>

        <Tooltip
          content={
            glossaryEmpty
              ? '词表为空：保存术语后这次翻译才会带上术语约束'
              : glossaryKnown
                ? `翻译时按全局词表约束译文（${glossaryCount} 条）；重译候选不带词表`
                : '翻译时按全局词表约束译文；词表条数未能读取'
          }
        >
          <label className="flex h-7 items-center gap-2 text-tiny text-ink-3">
            <input
              type="checkbox"
              data-od-id="start-job-use-glossary"
              checked={glossaryOn}
              disabled={glossaryEmpty}
              onChange={(event) => setUseGlossaryOverride(event.target.checked)}
            />
            使用词表
            {glossaryEmpty ? (
              <span className="text-ink-4" data-od-id="start-job-glossary-empty">
                （词表为空）
              </span>
            ) : glossaryKnown ? (
              <span className="font-mono text-ink-4">{glossaryCount} 条</span>
            ) : null}
          </label>
        </Tooltip>

        <details className="min-w-0">
          <summary className="cursor-pointer text-tiny text-ink-3">高级：起点阶段</summary>
          <label className="mt-1 flex flex-col gap-1">
            <select
              data-od-id="start-job-from"
              aria-label="起点阶段"
              value={from}
              onChange={(event) => setFromOverride(event.target.value)}
              className="h-7 rounded border border-hair bg-ivory px-2 font-mono text-sm text-ink-2"
            >
              {RUN_STAGES.map((stage) => (
                <option key={stage} value={stage}>
                  {stage} · {stageLabel(stage)}
                </option>
              ))}
            </select>
          </label>
        </details>

        <Button
          variant="primary"
          data-od-id="start-job-submit"
          disabled={!canSubmit}
          onClick={() =>
            createJob.mutate({
              action: 'run',
              from,
              pages: normalizePagesSpec(pages),
              dual,
              profile,
              // 词表只给布尔（缺省 true）：词表内容与注入用文件路径由服务端管。
              use_glossary: glossaryOn,
            })
          }
        >
          {createJob.isPending ? '正在提交…' : '开始翻译'}
        </Button>
      </div>

      {from === 'parse' ? (
        <p className="text-tiny text-ink-4" data-od-id="start-job-mineru-note">
          {MINERU_NOTE}
        </p>
      ) : null}
      {pagesValid ? null : (
        <p className="text-tiny text-err" data-od-id="start-job-pages-error">
          页码范围形状不对：只接受 1-3,5 这样的写法（留空表示全部页）。
        </p>
      )}
      {profilesQuery.isSuccess && profiles.length === 0 ? (
        <p className="text-tiny text-ink-4" data-od-id="start-job-no-profile">
          没有可用 profile：在 &lt;store_base&gt;/.bdt-serve/profiles.json 里配置，或用
          PUT /api/v1/profiles 写一条（只接受 scripts/ 白名单内的脚本路径引用）。
        </p>
      ) : null}
      {describe === null ? null : (
        <ErrorCard data-od-id="start-job-error" title={describe.title} message={describe.message} />
      )}
    </div>
  );
}
