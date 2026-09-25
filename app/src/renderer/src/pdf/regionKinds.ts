/**
 * 版面类型的中文名与叠加框颜色（原文 / 译文栏、图例、块详情共用）。
 */
import type { RegionKind } from '@shared/protocol';

export const REGION_STYLE: Record<RegionKind, { label: string; color: string }> = {
  title: { label: '标题', color: '#9f2f24' },
  paragraph_title: { label: '小节标题', color: '#b5541c' },
  abstract: { label: '摘要', color: '#8a5e16' },
  text: { label: '正文', color: '#2c6f75' },
  list: { label: '列表', color: '#3b6e9c' },
  caption: { label: '图表标题', color: '#6f3f82' },
  foot_note: { label: '脚注', color: '#7a6a3a' },
  reference: { label: '参考文献', color: '#4b6f3d' },
  formula: { label: '公式', color: '#b7791f' },
  table: { label: '表格', color: '#8d4f6c' },
  figure: { label: '图片', color: '#5b6470' },
  code: { label: '代码', color: '#34625a' },
  header: { label: '页眉', color: '#8d8575' },
  footer: { label: '页脚', color: '#8d8575' },
  other: { label: '其他', color: '#a39c8d' },
};
