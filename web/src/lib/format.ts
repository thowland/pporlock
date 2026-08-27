/** Display formatting for the flow table. */

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return '—';
  if (bytes < 1024) return `${bytes}`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}k`;
  return `${(bytes / (1024 * 1024)).toFixed(1)}M`;
}

export function formatMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return '—';
  if (ms < 1) return `${ms.toFixed(2)}`;
  if (ms < 1000) return `${ms.toFixed(1)}`;
  return `${(ms / 1000).toFixed(2)}s`;
}

/** Clock time, which is what you correlate against a page load. */
export function formatTime(iso: string): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return iso;
  const hh = String(at.getHours()).padStart(2, '0');
  const mm = String(at.getMinutes()).padStart(2, '0');
  const ss = String(at.getSeconds()).padStart(2, '0');
  const ms = String(at.getMilliseconds()).padStart(3, '0');
  return `${hh}:${mm}:${ss}.${ms}`;
}

export function statusClass(status: number | null | undefined): string {
  if (status === null || status === undefined) return 'status-none';
  if (status < 300) return 'status-2xx';
  if (status < 400) return 'status-3xx';
  if (status < 500) return 'status-4xx';
  return 'status-5xx';
}

/** Media type without parameters, shortened to the part that identifies it. */
export function shortType(contentType: string | null | undefined): string {
  if (!contentType) return '—';
  const media = contentType.split(';')[0]?.trim() ?? '';
  return media.replace(/^application\//, '').replace(/^text\//, '');
}
