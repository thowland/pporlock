import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { StatusBar } from './StatusBar';
import type { DaemonState } from '../api/types';

function makeState(overrides: Partial<DaemonState> = {}): DaemonState {
  return {
    version: '0.1.0',
    mitmproxy_version: '12.2.3',
    proxy: { running: true, listen: '127.0.0.1:8080', uptime_s: 100 },
    active_profile: 'default',
    dev_toggles: { anticache: false, anticomp: false },
    modules: { loaded: 0, enabled: 0, quarantined: 0, errors: [] },
    capture: { ring_flows: 12, ring_bytes: 2048, recording_session: null },
    counters: { flows_total: 42, blocked: 3, modified: 1, passthrough: 2, errors: 0 },
    clients: { mcp_connected: 0, mcp_read_only: false },
    ...overrides,
  };
}

describe('StatusBar', () => {
  it('reports live when connected and streaming', () => {
    render(
      <StatusBar state={makeState()} connection="connected" streamState="open" flowCount={5} />,
    );
    expect(screen.getByText('live')).toBeTruthy();
  });

  it('reports disconnected unmistakably', () => {
    render(<StatusBar state={null} connection="disconnected" streamState="closed" flowCount={0} />);
    expect(screen.getByText('disconnected')).toBeTruthy();
  });

  it('distinguishes unpaired from down', () => {
    render(<StatusBar state={null} connection="unauthorized" streamState="closed" flowCount={0} />);
    expect(screen.getByText('not paired')).toBeTruthy();
  });

  it('shows reconnecting rather than going quiet', () => {
    render(
      <StatusBar
        state={makeState()}
        connection="connected"
        streamState="reconnecting"
        flowCount={0}
      />,
    );
    expect(screen.getByText('reconnecting')).toBeTruthy();
  });

  it('shows counters', () => {
    render(
      <StatusBar state={makeState()} connection="connected" streamState="open" flowCount={5} />,
    );
    expect(screen.getByText('42')).toBeTruthy();
    expect(screen.getByText('3')).toBeTruthy();
  });

  it('makes an active development toggle prominent', () => {
    // REQ WUI-012, PXY-044 — these alter traffic in ways that make production
    // behaviour unreproducible, so they are never subtle.
    render(
      <StatusBar
        state={makeState({ dev_toggles: { anticache: true, anticomp: false } })}
        connection="connected"
        streamState="open"
        flowCount={0}
      />,
    );
    expect(screen.getByText(/anticache active/)).toBeTruthy();
  });

  it('names every active toggle', () => {
    render(
      <StatusBar
        state={makeState({ dev_toggles: { anticache: true, anticomp: true } })}
        connection="connected"
        streamState="open"
        flowCount={0}
      />,
    );
    expect(screen.getByText(/anticache \+ anticomp active/)).toBeTruthy();
  });

  it('says nothing about toggles when none are on', () => {
    render(
      <StatusBar state={makeState()} connection="connected" streamState="open" flowCount={0} />,
    );
    expect(screen.queryByText(/active/)).toBeNull();
  });

  it('renders before state has arrived', () => {
    render(
      <StatusBar state={null} connection="connected" streamState="connecting" flowCount={0} />,
    );
    expect(screen.getByText('pporlock')).toBeTruthy();
  });
});
