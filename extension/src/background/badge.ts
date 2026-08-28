/**
 * Toolbar badge (SPEC-3 §5.2, REQ EXT-012).
 *
 * The badge is the only part of pporlock a user sees without opening anything,
 * so it must be readable at a glance and must never look normal when it isn't.
 *
 * Sprint 5 shows global counts. Per-tab counts need attribution, which arrives
 * in Sprint 6 — a deliberate, stated partial rather than a quiet shortfall.
 */

export type BadgeState = 'off' | 'on' | 'counting' | 'dev_toggle' | 'warning' | 'error';

export interface BadgeView {
  text: string;
  /** Background colour. Shape and text also differ, so colour is never alone. */
  color: string;
  title: string;
}

const COLORS = {
  neutral: '#5a6472',
  accent: '#2c6fb5',
  warn: '#b07d18',
  error: '#b3261e',
} as const;

export interface BadgeCounts {
  blocked: number;
  modified: number;
  warnings: number;
  errors: number;
}

export function badgeView(
  state: BadgeState,
  counts: BadgeCounts = { blocked: 0, modified: 0, warnings: 0, errors: 0 },
): BadgeView {
  switch (state) {
    case 'off':
      return { text: '', color: COLORS.neutral, title: 'pporlock — proxy off' };
    case 'error':
      return {
        text: '!',
        color: COLORS.error,
        title:
          'pporlock turned the proxy off because the daemon stopped responding. ' +
          'Click for details.',
      };
    case 'dev_toggle':
      return {
        text: 'DEV',
        color: COLORS.warn,
        title:
          'A development toggle is active. Traffic is being altered in a way that ' +
          'makes normal behaviour unreproducible.',
      };
    case 'warning':
      return {
        text: String(counts.warnings || counts.blocked + counts.modified || ''),
        color: COLORS.warn,
        title: `pporlock — ${counts.warnings} warning(s) on this page`,
      };
    case 'counting': {
      const acted = counts.blocked + counts.modified;
      return {
        text: acted > 0 ? String(acted) : '',
        color: COLORS.accent,
        title: `pporlock — ${counts.blocked} blocked, ${counts.modified} modified`,
      };
    }
    case 'on':
    default:
      return { text: '', color: COLORS.accent, title: 'pporlock — proxy on' };
  }
}

/**
 * Which state applies, most severe first.
 *
 * Order matters: a user whose proxy just fell over does not need to be told
 * how many requests were modified before that happened.
 */
export function resolveBadgeState(input: {
  proxyEnabled: boolean;
  failSafeTripped: boolean;
  daemonReachable: boolean;
  devToggleActive: boolean;
  counts: BadgeCounts;
}): BadgeState {
  if (input.failSafeTripped) return 'error';
  if (!input.proxyEnabled) return 'off';
  if (!input.daemonReachable) return 'error';
  if (input.devToggleActive) return 'dev_toggle';
  if (input.counts.errors > 0 || input.counts.warnings > 0) return 'warning';
  if (input.counts.blocked + input.counts.modified > 0) return 'counting';
  return 'on';
}

export interface BadgeApi {
  setBadgeText(details: { text: string; tabId?: number }): Promise<void>;
  setBadgeBackgroundColor(details: { color: string; tabId?: number }): Promise<void>;
  setTitle(details: { title: string; tabId?: number }): Promise<void>;
}

export async function applyBadge(api: BadgeApi, view: BadgeView, tabId?: number): Promise<void> {
  const scope = tabId === undefined ? {} : { tabId };
  await api.setBadgeText({ text: view.text, ...scope });
  await api.setBadgeBackgroundColor({ color: view.color, ...scope });
  await api.setTitle({ title: view.title, ...scope });
}

export function chromeBadgeApi(): BadgeApi {
  return {
    setBadgeText: (details) => chrome.action.setBadgeText(details),
    setBadgeBackgroundColor: (details) => chrome.action.setBadgeBackgroundColor(details),
    setTitle: (details) => chrome.action.setTitle(details),
  };
}
