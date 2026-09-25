/**
 * 缩放档位步进。
 * @vitest-environment node
 */
import { describe, expect, it } from 'vitest';
import { stepZoom } from '@/store/workbench';

describe('stepZoom', () => {
  it('按档位上下步进，适合宽度视作 100%，两端封顶', () => {
    expect(stepZoom(1, 1)).toBe(1.25);
    expect(stepZoom(1.1, -1)).toBe(1);
    expect(stepZoom('fit-width', -1)).toBe(0.75);
    expect(stepZoom(3, 1)).toBe(3);
    expect(stepZoom(0.5, -1)).toBe(0.5);
  });
});
