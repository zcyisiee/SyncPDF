/**
 * 事件流文案：run_finished 的 ok 只表示质量检查全部通过，不代表译文有没有发布。
 */
import { describe, expect, it } from 'vitest';
import { describeEvent } from '@/panel/describeEvent';

describe('describeEvent run_finished', () => {
  // 回归：ok:false 显示"运行未完成"+错误色，与卡片上的"完成 · N 段回退"矛盾
  it('ok:false 是警告"质量检查未全部通过"，不是错误', () => {
    const line = describeEvent({ seq: 1, ts: 1, type: 'run_finished', ok: false, elapsed_ms: 94_500 });
    expect(line).toMatchObject({ tone: 'warning', text: '运行结束 · 质量检查未全部通过 · 94.5 秒' });
  });

  it('ok:true 是正常结束', () => {
    const line = describeEvent({ seq: 1, ts: 1, type: 'run_finished', ok: true, elapsed_ms: 1000 });
    expect(line).toMatchObject({ tone: 'ok', text: '运行结束 · 1.0 秒' });
  });
});
