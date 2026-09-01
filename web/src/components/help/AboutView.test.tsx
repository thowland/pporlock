/**
 * About.
 *
 * "Which version are you on" is the first question of every diagnosis this
 * project has, and it was worthless for eighteen sprints because the answer was
 * always 0.1.0 (OI-25). This view is where that question is answered, so what
 * is worth pinning is that it answers it — including when the daemon is not
 * there, which is exactly when someone goes looking.
 */
import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { AboutView } from './AboutView';
import { HOMEPAGE, LICENSE } from '../../lib/about';
import type { DaemonState } from '../../api/types';

const STATE = {
  version: '0.12.0',
  mitmproxy_version: '12.2.3',
  proxy: { running: true, listen: '127.0.0.1:8080', uptime_s: 60 },
  active_profile: 'default',
  dev_toggles: { anticache: false, anticomp: false },
  modules: { loaded: 2, enabled: 1, quarantined: 0, errors: [] },
  capture: { ring_flows: 3, ring_bytes: 90, recording_session: null },
  counters: { flows_total: 3, blocked: 0, modified: 0, passthrough: 0, errors: 0 },
  clients: { mcp_connected: 0, mcp_read_only: false },
} as unknown as DaemonState;

describe('AboutView', () => {
  it('shows the daemon and mitmproxy versions', () => {
    render(<AboutView state={STATE} onHelp={vi.fn()} />);
    expect(screen.getByText('0.12.0')).toBeTruthy();
    // mitmproxy is here because it is the dependency whose version changes
    // behaviour underneath us — normalize.py exists to absorb exactly that.
    expect(screen.getByText('12.2.3')).toBeTruthy();
  });

  it('shows where the proxy is listening when it is running', () => {
    render(<AboutView state={STATE} onHelp={vi.fn()} />);
    expect(screen.getByText('127.0.0.1:8080')).toBeTruthy();
  });

  it('still renders when the daemon has not answered', () => {
    render(<AboutView state={null} onHelp={vi.fn()} />);
    // Someone reading this page during an outage is the common case, not the
    // edge case: a blank about box tells them nothing about what they installed.
    expect(screen.getByText('not connected')).toBeTruthy();
    expect(screen.getByText(LICENSE)).toBeTruthy();
  });

  it('says plainly that it terminates TLS and can rewrite anything', () => {
    render(<AboutView state={STATE} onHelp={vi.fn()} />);
    expect(document.body.textContent).toMatch(/terminates TLS/);
    expect(document.body.textContent).toMatch(/unsandboxed/);
  });

  it('links to the project and to its issues', () => {
    render(<AboutView state={STATE} onHelp={vi.fn()} />);
    const hrefs = screen.getAllByRole('link').map((a) => a.getAttribute('href'));
    expect(hrefs).toContain(HOMEPAGE);
    expect(hrefs).toContain(`${HOMEPAGE}/issues`);
  });

  it('offers a way back to help', async () => {
    const onHelp = vi.fn();
    render(<AboutView state={STATE} onHelp={onHelp} />);
    await userEvent.click(screen.getByRole('button', { name: 'Help' }));
    expect(onHelp).toHaveBeenCalled();
  });
});
