/**
 * Hash routing (SPEC-2 §3.1).
 *
 * Hash rather than history because the daemon serves the UI from a static
 * directory on loopback and a hash route needs no SPA fallback rule at all —
 * one less thing that can differ between `vite dev` and the packaged build.
 *
 * The route vocabulary is closed: an unknown hash falls back to the traffic
 * view rather than rendering nothing, because a blank tool looks like a broken
 * daemon and this UI exists to make broken things visible.
 */
import { useCallback, useEffect, useState } from 'react';

export type Route =
  | { view: 'traffic' }
  | { view: 'modules' }
  | { view: 'module'; name: string }
  | { view: 'profiles' }
  | { view: 'newrule' };

export const DEFAULT_ROUTE: Route = { view: 'traffic' };

export function parseRoute(hash: string): Route {
  const path = hash.replace(/^#/, '').replace(/^\/+/, '');
  const segments = path.split('/').filter((s) => s.length > 0);
  const [head, second] = segments;
  if (head === 'modules') {
    if (second !== undefined && second !== '') {
      return { view: 'module', name: decodeURIComponent(second) };
    }
    return { view: 'modules' };
  }
  if (head === 'profiles') return { view: 'profiles' };
  if (head === 'newrule') return { view: 'newrule' };
  return DEFAULT_ROUTE;
}

export function routeToHash(route: Route): string {
  switch (route.view) {
    case 'modules':
      return '#/modules';
    case 'module':
      return `#/modules/${encodeURIComponent(route.name)}`;
    case 'profiles':
      return '#/profiles';
    case 'newrule':
      return '#/newrule';
    default:
      return '#/traffic';
  }
}

export function useHashRoute(): [Route, (next: Route) => void] {
  const [route, setRoute] = useState<Route>(() => parseRoute(window.location.hash));

  useEffect(() => {
    const onChange = () => setRoute(parseRoute(window.location.hash));
    window.addEventListener('hashchange', onChange);
    return () => window.removeEventListener('hashchange', onChange);
  }, []);

  const navigate = useCallback((next: Route) => {
    const hash = routeToHash(next);
    // Not a secret comparison: "hash" here is a URL fragment, which is why the
    // timing-attack heuristic fires. There is nothing to leak.
    // eslint-disable-next-line security/detect-possible-timing-attacks
    if (window.location.hash !== hash) window.location.hash = hash;
    // `hashchange` is asynchronous, and navigating to the hash already showing
    // fires nothing at all. Set state here so a click never appears to do
    // nothing; the listener above then re-derives the same value.
    setRoute(parseRoute(hash));
  }, []);

  return [route, navigate];
}
