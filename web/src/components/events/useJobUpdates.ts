/**
 * `job_update`（SSE 虚拟 kind，api.md §1.4.1）→ 缓存失效 + 兜底轮询节流（W14）。
 *
 * 为什么需要它：job 的生命周期事件（排队/运行/终态）不在 `events.jsonl` 里，W06 时代前端
 * 只能靠 2s 轮询发现「跑完了」——`compile`/`retranslate` 这类 job 的真 run 归档住在隔离
 * 副本里（真 workdir 里根本没有新 run），所以事件流对它们一点信号都没有。现在服务端状态一变
 * （先把状态写完盘）就在同一条 SSE 流里推一帧，前端按 `action` 分流失效：
 *
 *   run         → job 列表 + 详情 + 阶段状态 + 段落/候选 + 事件窗口 + 文档列表
 *   compile     → job 列表 + 详情 + 版本归档
 *   retranslate → job 列表 + 段落/候选
 *   终态（succeeded/failed/canceled/interrupted）再补：产物清单（+详情里的新 revision）
 *
 * 失效 = 立刻 refetch；同时把「刚推过」的时间戳记下来（`pushedAtMs`），交给 `useJobs` 的
 * 间隔函数在静默窗口内**跳过**兜底轮询（见 `lib/jobs.ts::withinJobUpdateQuietWindow`）——
 * 推送刚把真相送来（而且已经触发了一次 refetch），5s 内再轮询只是重复同一个请求。
 *
 * 轮询**永远不关**：推送不重放、且 job 建出 run 归档之前根本没有这条流（那时端点还是 404），
 * 只靠 SSE 会在丢帧/断线时静默不动。
 */
import { useCallback, useEffect, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';

import type { JobUpdate } from '../../lib/events';
import { JOBS_UPDATE_QUIET_MS, jobUpdateInvalidations } from '../../lib/jobs';

export interface JobUpdateFeed {
  /** 最近一次收到 `job_update` 的时刻（ms）；静默窗口内用它抑制兜底轮询。 */
  pushedAtMs: number | null;
  /** 交给 `useEventWindow`（→ `useEventStream`）的 `job_update` 回调。 */
  onJobUpdate: (update: JobUpdate) => void;
}

export function useJobUpdates(did: string | null): JobUpdateFeed {
  const client = useQueryClient();
  const [pushedAtMs, setPushedAtMs] = useState<number | null>(null);

  // 静默窗口到点后把时间戳清掉 → 组件重渲染 → `useJobs` 的间隔函数重新求值
  // （TanStack 在 options 变化时重启轮询计时器，见 `queryObserver.setOptions`）。
  // 这是「窗口结束」的唯一出口：没有它，轮询就会停在 `false` 上再不恢复。
  useEffect(() => {
    if (pushedAtMs === null) return;
    const timer = setTimeout(() => setPushedAtMs(null), JOBS_UPDATE_QUIET_MS);
    return () => clearTimeout(timer);
  }, [pushedAtMs]);

  const onJobUpdate = useCallback(
    (update: JobUpdate) => {
      if (did === null || did === '') return;
      setPushedAtMs(Date.now());
      for (const key of jobUpdateInvalidations(did, update)) {
        void client.invalidateQueries({ queryKey: key });
      }
    },
    [client, did],
  );

  return { pushedAtMs, onJobUpdate };
}
