import { render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { App } from './App';
import { ApiClient } from './api/client';
import { makeFlow } from './test/factories';

vi.mock('./api/events', () => ({
  EventStream: class {
    on() {
      return () => {};
    }
    onState() {
      return () => {};
    }
    connect() {}
    disconnect() {}
    get state() {
      return 'open';
    }
  },
}));

afterEach(() => vi.restoreAllMocks());

function api(): ApiClient {
  const client = new ApiClient('http://127.0.0.1:8081');
  vi.spyOn(client, 'getState').mockResolvedValue({
    version: '0.1.0',
    mitmproxy_version: '12.2.3',
    proxy: { running: true, listen: '127.0.0.1:8080', uptime_s: 1 },
    active_profile: 'default',
    dev_toggles: { anticache: false, anticomp: false },
    modules: { loaded: 0, enabled: 0, quarantined: 0, errors: [] },
    capture: { ring_flows: 1, ring_bytes: 10, recording_session: null },
    counters: { flows_total: 1, blocked: 0, modified: 0, passthrough: 0, errors: 0 },
    clients: { mcp_connected: 0, mcp_read_only: false },
  });
  vi.spyOn(client, 'listFlows').mockResolvedValue({
    flows: [makeFlow()],
    next_cursor: null,
    total_estimate: 1,
  });
  return client;
}

describe('App', () => {
  it('renders the shell, filters, and the table together', async () => {
    render(<App api={api()} />);
    expect(screen.getByText('pporlock')).toBeTruthy();
    expect(screen.getByLabelText('Filter by host')).toBeTruthy();
    await waitFor(() => expect(screen.getByText('cdn.example.com')).toBeTruthy());
  });

  it('shows the daemon listen address once state arrives', async () => {
    render(<App api={api()} />);
    await waitFor(() => expect(screen.getByText('127.0.0.1:8080')).toBeTruthy());
  });
});
