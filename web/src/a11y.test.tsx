/**
 * Accessibility pass (SPEC-2 §11, REQ WUI-015).
 *
 * These are the properties an automated pass can actually assert: every
 * interactive control has an accessible name, focus is managed on route change
 * and panel open/close, keyboard navigation reaches what a mouse reaches, and
 * meaning is never carried by colour alone. Contrast ratios and screen-reader
 * behaviour are checked by hand and in the Playwright suite; this file is the
 * regression net under them.
 */
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from './App';
import { ApiClient } from './api/client';
import { FlowTable } from './components/FlowTable';
import { ProvenanceView } from './components/detail/ProvenanceView';
import { makeFlow, makeProvenance, makeSession } from './test/factories';
import type { FlowRecord, ProvenanceNote } from './api/types';

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
  vi.spyOn(client, 'getFlow').mockResolvedValue(makeFlow());
  vi.spyOn(client, 'listModules').mockResolvedValue([]);
  vi.spyOn(client, 'listProfiles').mockResolvedValue([{ name: 'default', modules: [] }]);
  vi.spyOn(client, 'listSessions').mockResolvedValue([makeSession()]);
  vi.spyOn(client, 'getSession').mockResolvedValue(makeSession());
  vi.spyOn(client, 'listSessionFlows').mockResolvedValue({
    flows: [makeFlow()],
    next_cursor: null,
    total_estimate: 1,
  });
  vi.spyOn(client, 'getConfig').mockResolvedValue({
    redaction: { enabled: true, header_patterns: ['cookie'], json_key_patterns: ['token'] },
  });
  return client;
}

/** Every control an assistive technology can reach must have a name. */
function unnamedControls(root: HTMLElement): string[] {
  const controls = root.querySelectorAll('button, a[href], input, select, textarea');
  const nameless: string[] = [];
  for (const control of Array.from(controls)) {
    const text = (control.textContent ?? '').trim();
    const label = control.getAttribute('aria-label') ?? '';
    const labelledBy = control.getAttribute('aria-labelledby') ?? '';
    const title = control.getAttribute('title') ?? '';
    const id = control.getAttribute('id');
    const explicitLabel = id ? root.querySelector(`label[for="${id}"]`) : null;
    const wrappingLabel = control.closest('label');
    if (
      text === '' &&
      label === '' &&
      labelledBy === '' &&
      title === '' &&
      explicitLabel === null &&
      wrappingLabel === null
    ) {
      nameless.push(control.outerHTML.slice(0, 120));
    }
  }
  return nameless;
}

describe('accessible names  # REQ WUI-015', () => {
  it.each(['Traffic', 'Modules', 'Profiles', 'Sessions', 'Settings'])(
    'names every control on the %s view',
    async (view) => {
      const { container } = render(<App api={api()} />);
      await userEvent.click(screen.getByRole('button', { name: view }));
      await waitFor(() => expect(screen.getByRole('main')).toBeTruthy());
      expect(unnamedControls(container)).toEqual([]);
    },
  );

  it('names every control on the session browser and the detail panel', async () => {
    const { container } = render(<App api={api()} />);
    await userEvent.click(screen.getByRole('button', { name: 'Sessions' }));
    await userEvent.click(await screen.findByRole('button', { name: 'Browse' }));
    await userEvent.click(await screen.findByText('cdn.example.com'));
    await screen.findByLabelText('Flow detail');
    expect(unnamedControls(container)).toEqual([]);
  });
});

describe('focus management  # REQ WUI-015', () => {
  it('moves focus into the new view on a route change', async () => {
    // Otherwise a keyboard user stays parked on the nav button they pressed
    // while the whole page beneath them changed.
    render(<App api={api()} />);
    await userEvent.click(screen.getByRole('button', { name: 'Sessions' }));
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('main')));
  });

  it('moves focus into the detail panel when it opens, and back on close', async () => {
    render(<App api={api()} />);
    const row = (await screen.findByText('cdn.example.com')).closest('tr')!;
    row.focus();
    await userEvent.click(row);
    const panel = await screen.findByLabelText('Flow detail');
    await waitFor(() => expect(document.activeElement).toBe(panel));

    await userEvent.click(screen.getByRole('button', { name: 'Close detail' }));
    await waitFor(() => expect(document.activeElement).toBe(row));
  });

  it('closes the detail panel on Escape', async () => {
    render(<App api={api()} />);
    await userEvent.click(await screen.findByText('cdn.example.com'));
    await screen.findByLabelText('Flow detail');
    await userEvent.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByLabelText('Flow detail')).toBeNull());
  });
});

describe('keyboard navigation  # REQ WUI-015', () => {
  const flows: FlowRecord[] = [
    makeFlow({ flow_id: 'a' }),
    makeFlow({ flow_id: 'b' }),
    makeFlow({ flow_id: 'c' }),
  ];

  it('walks table rows with the arrow keys', async () => {
    // Tab alone would stop at every action button on the way down.
    const { container } = render(<FlowTable flows={flows} connected hasFilter={false} />);
    const rows = Array.from(container.querySelectorAll('tbody tr')) as HTMLElement[];
    rows[0]!.focus();
    await userEvent.keyboard('{ArrowDown}');
    expect(document.activeElement).toBe(rows[1]);
    await userEvent.keyboard('{ArrowDown}');
    expect(document.activeElement).toBe(rows[2]);
    await userEvent.keyboard('{ArrowUp}');
    expect(document.activeElement).toBe(rows[1]);
  });

  it('stops at the ends rather than wrapping', async () => {
    const { container } = render(<FlowTable flows={flows} connected hasFilter={false} />);
    const rows = Array.from(container.querySelectorAll('tbody tr')) as HTMLElement[];
    rows[0]!.focus();
    await userEvent.keyboard('{ArrowUp}');
    expect(document.activeElement).toBe(rows[0]);
  });

  it('opens a row with Enter and with Space', async () => {
    const opened: string[] = [];
    const { container } = render(
      <FlowTable
        flows={flows}
        connected
        hasFilter={false}
        onSelect={(flow) => opened.push(flow.flow_id)}
      />,
    );
    const rows = Array.from(container.querySelectorAll('tbody tr')) as HTMLElement[];
    rows[0]!.focus();
    await userEvent.keyboard('{Enter}');
    rows[1]!.focus();
    await userEvent.keyboard(' ');
    expect(opened).toEqual(['a', 'b']);
  });
});

describe('meaning is never carried by colour alone  # REQ WUI-015', () => {
  it('gives every flow flag a word as well as a colour', () => {
    const flow = makeFlow({
      blocked: true,
      modified: true,
      tab_id: null,
      provenance: makeProvenance({
        notes: [
          { code: 'module_error', severity: 'error', message: 'raised' },
          { code: 'csp_modified', severity: 'warning', message: 'relaxed' },
        ] as ProvenanceNote[],
      }),
    });
    const attributed = makeFlow({ flow_id: 'z', tab_id: 7 });
    render(<FlowTable flows={[flow, attributed]} connected hasFilter={false} />);
    // BLK/MOD are already words; the symbol-only flags carry one too.
    expect(screen.getByText('has error notes')).toBeTruthy();
    expect(screen.getByText('unattributed')).toBeTruthy();
  });

  it('labels a provenance note with its severity in words', () => {
    render(
      <ProvenanceView
        provenance={makeProvenance({
          notes: [
            { code: 'module_error', severity: 'error', message: 'raised' },
            { code: 'csp_modified', severity: 'warning', message: 'relaxed' },
            { code: 'dev_toggle_active', severity: 'info', message: 'anticomp' },
          ] as ProvenanceNote[],
        })}
      />,
    );
    for (const severity of ['error', 'warning', 'info']) {
      expect(screen.getByText(severity)).toBeTruthy();
    }
  });

  it('states a non-applied outcome in words, not only in red', () => {
    render(
      <ProvenanceView
        provenance={makeProvenance({
          entries: [
            {
              seq: 0,
              phase: 'response_body',
              module: 'strip-sri',
              rule_id: 'r1',
              rule_name: 'strip',
              action: 'body',
              outcome: 'skipped_streamed',
              duration_ms: 0.1,
              detail: {},
            },
          ],
        } as Parameters<typeof makeProvenance>[0])}
      />,
    );
    const outcome = screen.getByText(/skipped — response streamed/);
    expect(outcome).toBeTruthy();
    // The mark is decorative and hidden from assistive technology; the words
    // are what carry the meaning.
    expect(within(outcome).getByText('⚠').getAttribute('aria-hidden')).toBe('true');
  });
});
