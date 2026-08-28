import { act, renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { DEFAULT_ROUTE, parseRoute, routeToHash, useHashRoute } from './router';

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
