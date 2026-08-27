import { renderHook, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiClient, ApiRequestError } from '../api/client';
import { useDaemonState } from './useDaemonState';

afterEach(() => vi.unstubAllGlobals());

function apiWith(impl: () => Promise<unknown>): ApiClient {
  const api = new ApiClient('http://127.0.0.1:8081');
  vi.spyOn(api, 'getState').mockImplementation(impl as never);
  return api;
}

describe('useDaemonState', () => {
  it('reports connected once state arrives', async () => {
    const api = apiWith(async () => ({ version: '0.1.0' }));
    const { result } = renderHook(() => useDaemonState(api));
    await waitFor(() => expect(result.current.connection).toBe('connected'));
    expect(result.current.state).toBeTruthy();
  });

  it('distinguishes unauthorized from unreachable', async () => {
    // Three states look identical in a naive client: down, up-but-unpaired, and
    // working. Conflating them makes the disconnected banner useless.
    const api = apiWith(async () => {
      throw new ApiRequestError(401, 'unauthorized', 'no token');
    });
    const { result } = renderHook(() => useDaemonState(api));
    await waitFor(() => expect(result.current.connection).toBe('unauthorized'));
  });

  it('reports disconnected on a network failure', async () => {
    const api = apiWith(async () => {
      throw new TypeError('Failed to fetch');
    });
    const { result } = renderHook(() => useDaemonState(api));
    await waitFor(() => expect(result.current.connection).toBe('disconnected'));
  });

  it('recovers when the daemon comes back', async () => {
    let fail = true;
    const api = apiWith(async () => {
      if (fail) throw new TypeError('down');
      return { version: '0.1.0' };
    });
    const { result } = renderHook(() => useDaemonState(api));
    await waitFor(() => expect(result.current.connection).toBe('disconnected'));
    fail = false;
    result.current.refresh();
    await waitFor(() => expect(result.current.connection).toBe('connected'));
  });
});
