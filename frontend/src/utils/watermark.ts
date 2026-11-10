import type { SyncRunBrief } from '../types/models';

/** 时间戳 / 自增 ID / 字符串三种形态的水位线文案与比较 */
export type WatermarkKind = 'timestamp' | 'numeric' | 'text';

export const watermarkKind = (value: string | number | null | undefined): WatermarkKind => {
  if (value === null || value === undefined || value === '') return 'text';
  const text = String(value);
  if (/^\d+$/.test(text)) return 'numeric';
  if (/^\d{4}-\d{2}-\d{2}/.test(text)) return 'timestamp';
  return 'text';
};

export const formatWatermark = (value: string | number | null | undefined): string => {
  if (value === null || value === undefined || value === '') return '（初始）';
  if (watermarkKind(value) === 'timestamp') {
    return String(value).replace('T', ' ').slice(0, 19);
  }
  return String(value);
};

export const watermarkLine = (from: string | null, to: string | null, rowsWritten = 0): string => {
  if (formatWatermark(from) === formatWatermark(to)) {
    // 水位线没动但有写入：说明是迟到补传行按业务键归并进来了
    if (rowsWritten > 0) {
      return `水位线停在 ${formatWatermark(to)}（迟到行已按业务键归并）`;
    }
    return `水位线停在 ${formatWatermark(to)}（本批无新数据）`;
  }
  return `水位线 ${formatWatermark(from)} → ${formatWatermark(to)}`;
};

export const isNoNewData = (run: Pick<SyncRunBrief, 'rows_written' | 'rows_rejected'>): boolean =>
  run.rows_written === 0 && run.rows_rejected === 0;

export const compareWatermark = (left: string, right: string): number => {
  const kind = watermarkKind(left);
  if (kind === 'numeric') return Number(left) - Number(right);
  if (kind === 'timestamp') return new Date(left).getTime() - new Date(right).getTime();
  return left.localeCompare(right);
};
