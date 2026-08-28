/** Badge state. SPEC-3 §5.2, REQ EXT-012. */
import { describe, expect, it } from 'vitest';
import { applyBadge, badgeView, resolveBadgeState } from './badge';
import { FakeBadgeApi } from '../test/fakes';

const NONE = { blocked: 0, modified: 0, warnings: 0, errors: 0 };

describe('resolveBadgeState — most severe wins', () => {
  it('a tripped fail-safe outranks everything', () => {
    // A user whose proxy just fell over does not need a modification count.
    expect(
      resolveBadgeState({
        proxyEnabled: true,
        failSafeTripped: true,
        daemonReachable: true,
        devToggleActive: true,
        counts: { blocked: 9, modified: 9, warnings: 9, errors: 9 },
      }),
    ).toBe('error');
  });

  it('off when the proxy is off', () => {
    expect(
      resolveBadgeState({
        proxyEnabled: false,
        failSafeTripped: false,
        daemonReachable: true,
        devToggleActive: false,
        counts: NONE,
      }),
    ).toBe('off');
  });

  it('error when the daemon is unreachable while enabled', () => {
    expect(
      resolveBadgeState({
        proxyEnabled: true,
        failSafeTripped: false,
        daemonReachable: false,
        devToggleActive: false,
        counts: NONE,
      }),
    ).toBe('error');
  });

  it('dev toggle outranks ordinary counts', () => {
    expect(
      resolveBadgeState({
        proxyEnabled: true,
        failSafeTripped: false,
        daemonReachable: true,
        devToggleActive: true,
        counts: { blocked: 5, modified: 5, warnings: 0, errors: 0 },
      }),
    ).toBe('dev_toggle');
  });

  it('warning when notes are present', () => {
    expect(
      resolveBadgeState({
        proxyEnabled: true,
        failSafeTripped: false,
        daemonReachable: true,
        devToggleActive: false,
        counts: { blocked: 0, modified: 0, warnings: 2, errors: 0 },
      }),
    ).toBe('warning');
  });

  it('counting when something was acted on', () => {
    expect(
      resolveBadgeState({
        proxyEnabled: true,
        failSafeTripped: false,
        daemonReachable: true,
        devToggleActive: false,
        counts: { blocked: 3, modified: 0, warnings: 0, errors: 0 },
      }),
    ).toBe('counting');
  });

  it('plain on when nothing has happened yet', () => {
    expect(
      resolveBadgeState({
        proxyEnabled: true,
        failSafeTripped: false,
        daemonReachable: true,
        devToggleActive: false,
        counts: NONE,
      }),
    ).toBe('on');
  });
});

describe('badgeView', () => {
  it('off shows no text', () => {
    expect(badgeView('off').text).toBe('');
  });

  it('error is unmistakable and says what happened', () => {
    const view = badgeView('error');
    expect(view.text).toBe('!');
    expect(view.title).toMatch(/turned the proxy off/);
  });

  it('dev toggle is labelled, not just coloured', () => {
    // Colour alone is not enough; the text carries the meaning too.
    const view = badgeView('dev_toggle');
    expect(view.text).toBe('DEV');
    expect(view.title).toMatch(/unreproducible/);
  });

  it('counting shows the total acted on', () => {
    expect(badgeView('counting', { blocked: 2, modified: 3, warnings: 0, errors: 0 }).text).toBe(
      '5',
    );
  });

  it('counting with nothing acted on shows no number', () => {
    expect(badgeView('counting', NONE).text).toBe('');
  });

  it('warning names the count in its tooltip', () => {
    expect(badgeView('warning', { blocked: 0, modified: 0, warnings: 4, errors: 0 }).title).toMatch(
      /4 warning/,
    );
  });

  it('every state produces a title', () => {
    for (const state of ['off', 'on', 'counting', 'dev_toggle', 'warning', 'error'] as const) {
      expect(badgeView(state).title.length).toBeGreaterThan(0);
    }
  });
});

describe('applyBadge', () => {
  it('sets text, colour, and title together', async () => {
    const api = new FakeBadgeApi();
    await applyBadge(api, badgeView('error'));
    expect(api.last.text).toBe('!');
    expect(api.last.color).toBeTruthy();
    expect(api.last.title).toBeTruthy();
  });

  it('scopes to a tab when given one', async () => {
    const api = new FakeBadgeApi();
    await applyBadge(api, badgeView('on'), 7);
    expect(api.last.title).toBeTruthy();
  });
});
