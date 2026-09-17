import { STAGE_LABELS, STAGE_NAMES } from '../../lib/humanize';

/**
 * 底部时间线占位（§8.3 两行结构：元信息行 + 名称行 + 时长条行）。
 * 本任务是**静态非真数据**：7 段名称 + 未开始态灰条（64px + 1px dashed `--hair-2`）；
 * 真实阶段耗时与事件流在 W06 接入，届时条宽改由同一 px/秒 标尺驱动。
 */
export function Timeline({ did }: { did: string }) {
  return (
    <section
      aria-label="阶段时间线"
      data-od-id="timeline"
      className="col-span-full row-start-3 flex min-w-0 flex-col gap-[5px] overflow-hidden border-t border-hair bg-ivory px-s4 pb-[6px] pt-[7px]"
    >
      <div className="flex h-5 flex-none items-center gap-s4 whitespace-nowrap font-mono text-micro tracking-[0.03em] text-ink-4">
        <span className="min-w-0 truncate">
          <b className="font-medium text-ink-2">{did}</b> · 阶段时间线
        </span>
        <span className="ml-auto flex-none">静态占位 · 真实事件流 W06 接入</span>
      </div>
      <div className="flex min-h-0 flex-1 overflow-x-auto overflow-y-hidden">
        <ol className="grid flex-1 grid-cols-[repeat(7,minmax(136px,1fr))] gap-x-[10px]">
          {STAGE_NAMES.map((stage) => (
            <li
              key={stage}
              data-od-id={`timeline-stage-${stage}`}
              className="grid min-w-0 grid-rows-[minmax(0,1fr)_14px] gap-y-[3px]"
            >
              <span className="flex min-w-0 flex-col justify-center">
                <span className="flex items-center gap-[5px] whitespace-nowrap text-tiny font-medium leading-[1.4] tracking-[0.02em] text-ink-4">
                  {STAGE_LABELS[stage]}
                </span>
                <span aria-hidden="true" className="mt-[2px] h-[5px] w-px flex-none bg-hair-2" />
              </span>
              <span className="flex h-[14px] items-end border-b border-hair">
                <span
                  aria-hidden="true"
                  className="h-[10px] w-16 flex-none rounded-t-[2px] border border-dashed border-b-0 border-hair-2"
                />
              </span>
            </li>
          ))}
        </ol>
      </div>
    </section>
  );
}
