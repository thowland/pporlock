/**
 * Daemon status polling (SPEC-2 §3.2).
 *
 * Health is polled unauthenticated so the UI can distinguish three states that
 * look identical in a naive client: daemon down, daemon up but unpaired, and
 * daemon up and working.
 */
import { useCallback, useEffect, useState } from 'react';
import type { ApiClient } from '../api/client';
import { ApiRequestError } from '../api/client';
import type { DaemonState } from '../api/types';

const POLL_MS = 3000;

export type Connection = 'connected' | 'unauthorized' | 'disconnected';

export function useDaemonState(api: ApiClient): {
  state: DaemonState | null;
  connection: Connection;
  refresh: () => void;
} {
  const [state, setState] = useState<DaemonState | null>(null);
  const [connection, setConnection] = useState<Connection>('disconnected');

  const refresh = useCallback(() => {
    void api
      .getState()
      .then((next) => {
        setState(next);
        setConnection('connected');
      })
      .catch((error: unknown) => {
        if (error instanceof ApiRequestError && error.status === 401) {
          setConnection('unauthorized');
        } else {
          setConnection('disconnected');
        }
      });
  }, [api]);

  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, POLL_MS);
    return () => clearInterval(timer);
  }, [refresh]);

  return { state, connection, refresh };
}
