/**
 * 回归：译文重载时页面高度变化会触发 scroll 事件，旧实现把它当成用户滚动广播，
 * 原文栏被拖到别处。现在只有用户输入之后的滚动才广播。
 * @vitest-environment node
 */
import { describe, expect, it } from 'vitest';
import { USER_SCROLL_WINDOW_MS, UserScrollGate } from '@/pdf/scrollSync';

function gateAt(start = 1000): { gate: UserScrollGate; advance: (ms: number) => void } {
  let now = start;
  return { gate: new UserScrollGate(() => now), advance: (ms) => (now += ms) };
}

describe('UserScrollGate', () => {
  it('没有用户输入时的滚动（布局变化 / 对侧同步 / 跳转）不算用户滚动', () => {
    const { gate } = gateAt();
    expect(gate.isUserScroll()).toBe(false);
  });

  it('滚轮或键盘输入后的一段时间内算用户滚动，过期后不算', () => {
    const { gate, advance } = gateAt();
    gate.noteInput();
    expect(gate.isUserScroll()).toBe(true);
    advance(USER_SCROLL_WINDOW_MS + 1);
    expect(gate.isUserScroll()).toBe(false);
  });

  it('按住滚动条拖动期间一直算，松开后按输入窗口延续', () => {
    const { gate, advance } = gateAt();
    gate.setPointerDown(true);
    advance(10 * USER_SCROLL_WINDOW_MS);
    expect(gate.isUserScroll()).toBe(true);
    gate.setPointerDown(false);
    expect(gate.isUserScroll()).toBe(true);
    advance(USER_SCROLL_WINDOW_MS + 1);
    expect(gate.isUserScroll()).toBe(false);
  });
});
