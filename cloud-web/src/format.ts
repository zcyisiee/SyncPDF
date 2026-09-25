// 时间、大小等展示格式。

const pad = (n: number) => String(n).padStart(2, '0');

/** 秒级时间戳 → HH:MM */
export function clock(ts: number): string {
  const d = new Date(ts * 1000);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function size(bytes: number): string {
  if (bytes >= 1 << 20) return `${(bytes / (1 << 20)).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

export function duration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  return s >= 60 ? `${Math.floor(s / 60)} 分 ${pad(s % 60)} 秒` : `${s} 秒`;
}

function startOfDay(d: Date): number {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
}

/** 历史列表分组与时间：今天 / 昨天 / 更早。 */
export function when(ts: number, now = new Date()): { group: string; label: string } {
  const d = new Date(ts * 1000);
  const days = Math.round((startOfDay(now) - startOfDay(d)) / 86_400_000);
  const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  if (days <= 0) return { group: '今天', label: `今天 ${hm}` };
  if (days === 1) return { group: '更早', label: `昨天 ${hm}` };
  return { group: '更早', label: `${d.getMonth() + 1}月${d.getDate()}日` };
}

export function pageList(pages: number[]): string {
  return pages.join('、');
}
