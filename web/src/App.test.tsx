import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
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

beforeEach(() => {
  window.location.hash = '';
});

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
  vi.spyOn(client, 'listModules').mockResolvedValue({ modules: [] });
  vi.spyOn(client, 'listProfiles').mockResolvedValue({
    profiles: [{ name: 'default', modules: [] }],
    active: 'default',
  });
  vi.spyOn(client, 'suggestRule').mockResolvedValue({
    rule: { name: 'block-cdn-example-com', action: 'block', match: { host: 'cdn.example.com' } },
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

  it('keeps traffic as the default view  # SPEC-2 §3.1', async () => {
    render(<App api={api()} />);
    await waitFor(() => expect(screen.getByText('cdn.example.com')).toBeTruthy());
    expect(screen.getByRole('button', { name: 'Traffic' }).getAttribute('aria-current')).toBe(
      'page',
    );
  });

  it('navigates to the module library and back  # REQ WUI-005', async () => {
    render(<App api={api()} />);
    await userEvent.click(screen.getByRole('button', { name: 'Modules' }));
    await waitFor(() => expect(screen.getByText('No modules')).toBeTruthy());
    expect(window.location.hash).toBe('#/modules');

    await userEvent.click(screen.getByRole('button', { name: 'Traffic' }));
    await waitFor(() => expect(screen.getByText('cdn.example.com')).toBeTruthy());
  });

  it('navigates to profiles  # REQ WUI-009', async () => {
    render(<App api={api()} />);
    await userEvent.click(screen.getByRole('button', { name: 'Profiles' }));
    await waitFor(() => expect(screen.getByLabelText('New profile name')).toBeTruthy());
  });

  it('opens the module editor from a deep link', async () => {
    const client = api();
    vi.spyOn(client, 'getModule').mockResolvedValue({
      name: 'block-vendors',
      version: '1.0.0',
      enabled: false,
      priority: 100,
      state: 'loaded',
      has_python: false,
      rule_count: 0,
      error: null,
      quarantine: null,
      stats: { flows_matched: 0, flows_modified: 0, errors: 0, avg_ms: 0 },
      files: { 'module.yaml': 'name: block-vendors\n' },
    });
    window.location.hash = '#/modules/block-vendors';
    render(<App api={client} />);
    await waitFor(() => expect(screen.getByRole('tab', { name: 'module.yaml' })).toBeTruthy());
  });

  // REQ WUI-008: two clicks from the table to a pre-filled rule in the builder.
  it('reaches a pre-filled rule builder in two clicks from a flow row', async () => {
    render(<App api={api()} />);
    await waitFor(() => expect(screen.getByText('cdn.example.com')).toBeTruthy());

    await userEvent.click(screen.getByRole('button', { name: /Create rule from flow/ }));
    await userEvent.click(screen.getByRole('menuitem', { name: 'Block' }));

    await waitFor(() =>
      expect(screen.getByLabelText('Rule name')).toHaveProperty('value', 'block-cdn-example-com'),
    );
    expect(screen.getByLabelText('Host')).toHaveProperty('value', 'cdn.example.com');
  });

  it('returns to traffic when the pre-filled rule is abandoned', async () => {
    render(<App api={api()} />);
    await waitFor(() => expect(screen.getByText('cdn.example.com')).toBeTruthy());
    await userEvent.click(screen.getByRole('button', { name: /Create rule from flow/ }));
    await userEvent.click(screen.getByRole('menuitem', { name: 'Block' }));
    await screen.findByLabelText('Rule name');
    await userEvent.click(screen.getByRole('button', { name: '← Traffic' }));
    await waitFor(() => expect(screen.getByText('cdn.example.com')).toBeTruthy());
  });

  it('falls back to traffic if /newrule is opened with no rule in hand', async () => {
    window.location.hash = '#/newrule';
    render(<App api={api()} />);
    await waitFor(() => expect(screen.getByText('cdn.example.com')).toBeTruthy());
  });
});
