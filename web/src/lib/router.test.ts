import { act, renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { DEFAULT_ROUTE, parseRoute, routeToHash, useHashRoute } from './router';
import type { Route } from './router';

describe('parseRoute', () => {
  it('maps every route in SPEC-2 §3.1 that this sprint implements', () => {
    expect(parseRoute('#/traffic')).toEqual({ view: 'traffic' });
    expect(parseRoute('#/modules')).toEqual({ view: 'modules' });
    expect(parseRoute('#/modules/')).toEqual({ view: 'modules' });
    expect(parseRoute('#/modules/block-vendors')).toEqual({
      view: 'module',
      name: 'block-vendors',
    });
    expect(parseRoute('#/profiles')).toEqual({ view: 'profiles' });
    expect(parseRoute('#/newrule')).toEqual({ view: 'newrule' });
  });

  it('decodes a module name that needed escaping', () => {
    expect(parseRoute('#/modules/a%2Fb')).toEqual({ view: 'module', name: 'a/b' });
  });

  it('falls back to traffic rather than rendering nothing', () => {
    expect(parseRoute('')).toEqual(DEFAULT_ROUTE);
    expect(parseRoute('#/nope')).toEqual(DEFAULT_ROUTE);
  });
});

describe('routeToHash', () => {
  it('round-trips every route', () => {
    for (const route of [
      { view: 'traffic' },
      { view: 'modules' },
      { view: 'module', name: 'block/vendors' },
      { view: 'profiles' },
      { view: 'newrule' },
    ] as const) {
      expect(parseRoute(routeToHash(route))).toEqual(route);
    }
  });
});

describe('useHashRoute', () => {
  beforeEach(() => {
    window.location.hash = '';
  });

  it('navigates and follows external hash changes', () => {
    const { result } = renderHook(() => useHashRoute());
    expect(result.current[0]).toEqual({ view: 'traffic' });

    act(() => result.current[1]({ view: 'modules' }));
    expect(window.location.hash).toBe('#/modules');
    expect(result.current[0]).toEqual({ view: 'modules' });
  });

  it('updates even when asked to navigate to the hash already showing', () => {
    window.location.hash = '#/profiles';
    const { result } = renderHook(() => useHashRoute());
    act(() => result.current[1]({ view: 'profiles' }));
    expect(result.current[0]).toEqual({ view: 'profiles' });
  });
});

describe('session and settings routes  # REQ WUI-010, WUI-011', () => {
  it('parses the session list, one session, and its dry run', () => {
    expect(parseRoute('#/sessions')).toEqual({ view: 'sessions' });
    expect(parseRoute('#/sessions/s1')).toEqual({ view: 'session', id: 's1' });
    // Dry run hangs off the session it runs against — it is meaningless
    // without one, and the URL should say which (SPEC-2 §8.3).
    expect(parseRoute('#/sessions/s1/dryrun')).toEqual({ view: 'dryrun', id: 's1' });
    expect(parseRoute('#/settings')).toEqual({ view: 'settings' });
  });

  it('decodes a session id that needed escaping', () => {
    expect(parseRoute('#/sessions/a%20b')).toEqual({ view: 'session', id: 'a b' });
  });

  it('treats an unknown third segment as the session itself', () => {
    expect(parseRoute('#/sessions/s1/nonsense')).toEqual({ view: 'session', id: 's1' });
  });

  it('round-trips every new route through the hash', () => {
    const routes: Route[] = [
      { view: 'sessions' },
      { view: 'session', id: 's 1' },
      { view: 'dryrun', id: 's 1' },
      { view: 'settings' },
    ];
    for (const route of routes) {
      expect(parseRoute(routeToHash(route))).toEqual(route);
    }
  });
});

describe('help and about  # the routes the extension links into', () => {
  it('parses both', () => {
    expect(parseRoute('#/help')).toEqual({ view: 'help' });
    expect(parseRoute('#/about')).toEqual({ view: 'about' });
  });

  it('round-trips both through the hash', () => {
    // These two are a contract, not just internal navigation: the extension's
    // about page links to `#/help` and `#/about` from a separately-built
    // artefact, so renaming either breaks a build nothing here compiles.
    for (const route of [{ view: 'help' }, { view: 'about' }] as Route[]) {
      expect(parseRoute(routeToHash(route))).toEqual(route);
    }
  });

  it('still falls back to traffic for a hash near but not equal to them', () => {
    expect(parseRoute('#/helping')).toEqual({ view: 'traffic' });
  });
});
