/**
 * The DevTools panel. SPEC-3 §7, REQ EXT-013.
 *
 * The completeness tests mirror the web UI's. The two render provenance from
 * separate tables — they are separately built with no shared package — and
 * these are what stop them drifting into disagreement about what an outcome
 * means.
 */
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ControlApi } from '../shared/api';
import type { FlowRecord } from '../shared/flows';
import { PanelView, severityOf } from './PanelView';
import {
  NEGATIVE_OUTCOMES,
  NOTE_LABEL,
  OUTCOME_LABEL,
  PHASE_LABEL,
  PHASE_ORDER,
} from './provenance';

const ALL_OUTCOMES = [
  'applied',
  'no_change',
  'skipped_streamed',
  'skipped_budget',
  'skipped_short_circuit',
  'skipped_disabled',
  'error',
] as const;

const ALL_NOTE_CODES = [
  'response_streamed',
  'transform_budget_exceeded',
  'module_quarantined',
  'map_local_missing',
  'csp_modified',
  'sri_stripped',
  'script_injected',
  'dev_toggle_active',
  'body_truncated',
  'module_error',
  'passthrough_excluded',
  'attribution_missing',
  'module_deprecation',
] as const;

function flow(overrides: Record<string, unknown> = {}): FlowRecord {
  return {
    flow_id: 'f0',
    kind: 'http',
    started_at: '2026-08-27T14:00:00.000Z',
    tab_id: 7,
    modified: false,
    blocked: false,
    redacted: false,
    request: {
      method: 'GET',
      scheme: 'https',
      host: 'cdn.example.com',
      port: 443,
      path: '/a.js',
      query: [],
      url: 'https://cdn.example.com/a.js',
      http_version: 'HTTP/2',
      dest: 'script',
      headers: [],
      body_size: 0,
      body_truncated: false,
    },
    response: {
      status: 200,
      reason: 'OK',
      http_version: 'HTTP/2',
      headers: [],
      content_type: 'application/javascript',
      body_size: 10,
      body_truncated: false,
      streamed: false,
    },
    provenance: {
      profile: 'default',
      evaluated_modules: [],
      entries: [],
      notes: [],
      total_ms: 1,
      short_circuited_by: null,
    },
    ...overrides,
  } as unknown as FlowRecord;
}

function apiWith(flows: FlowRecord[]): ControlApi {
  const api = new ControlApi('http://127.0.0.1:8081');
  vi.spyOn(api, 'listFlows').mockResolvedValue({
    flows,
    next_cursor: null,
    total_estimate: flows.length,
  });
  return api;
}

afterEach(() => vi.restoreAllMocks());

describe('completeness — mirrors the web UI so the two cannot disagree', () => {
  it.each(PHASE_ORDER)('phase %s has a label', (phase) => {
    expect(PHASE_LABEL[phase]).toBeTruthy();
  });

  it.each(ALL_OUTCOMES)('outcome %s has a label', (outcome) => {
    expect(OUTCOME_LABEL[outcome]).toBeTruthy();
  });

  it.each(ALL_NOTE_CODES)('note code %s has a meaning', (code) => {
    expect(NOTE_LABEL[code]).toBeTruthy();
  });

  it('classifies every non-applied outcome as negative', () => {
    for (const outcome of ALL_OUTCOMES) {
      if (outcome === 'applied' || outcome === 'no_change') continue;
      expect(NEGATIVE_OUTCOMES.has(outcome)).toBe(true);
    }
  });
});

describe('severityOf', () => {
  it('is null for a clean flow', () => {
    expect(severityOf(flow())).toBeNull();
  });

  it('reports a warning', () => {
    const f = flow();
    f.provenance!.notes = [{ code: 'csp_modified', severity: 'warning', message: 'x' }] as never;
    expect(severityOf(f)).toBe('warning');
  });

  it('lets an error outrank a warning', () => {
    const f = flow();
    f.provenance!.notes = [
      { code: 'csp_modified', severity: 'warning', message: 'w' },
      { code: 'module_error', severity: 'error', message: 'e' },
    ] as never;
    expect(severityOf(f)).toBe('error');
  });
});

describe('PanelView', () => {
  it('lists flows for the inspected tab', async () => {
    const api = apiWith([flow()]);
    render(<PanelView api={api} tabId={7} pollMs={999_999} />);
    await waitFor(() => expect(screen.getByText('cdn.example.com')).toBeTruthy());
  });

  it('scopes the query to the inspected tab', async () => {
    // "What did pporlock do to *this page*" is the question the panel answers.
    const api = apiWith([]);
    render(<PanelView api={api} tabId={42} pollMs={999_999} />);
    await waitFor(() =>
      expect(api.listFlows).toHaveBeenCalledWith(expect.objectContaining({ tab_id: 42 })),
    );
  });

  it('says what to do when a tab has no flows yet', async () => {
    render(<PanelView api={apiWith([])} tabId={7} pollMs={999_999} />);
    await waitFor(() => expect(screen.getByText(/Reload the page with the proxy on/)).toBeTruthy());
  });

  it('reports an unreachable daemon rather than showing an empty table', async () => {
    const api = new ControlApi('http://127.0.0.1:8081');
    vi.spyOn(api, 'listFlows').mockRejectedValue(new Error('down'));
    render(<PanelView api={api} tabId={7} pollMs={999_999} />);
    await waitFor(() => expect(screen.getByText(/Cannot reach the daemon/)).toBeTruthy());
  });

  it('shows provenance when a flow is selected', async () => {
    const f = flow();
    f.provenance!.entries = [
      {
        seq: 0,
        phase: 'request_short_circuit',
        module: 'block-vendors',
        rule_id: 'block-vendors:2',
        rule_name: 'block-analytics-vendor',
        action: 'block',
        outcome: 'applied',
        duration_ms: 0.3,
        detail: {},
      },
    ] as never;
    render(<PanelView api={apiWith([f])} tabId={7} pollMs={999_999} />);
    await waitFor(() => expect(screen.getByText('cdn.example.com')).toBeTruthy());
    await userEvent.click(screen.getByText('cdn.example.com'));
    expect(screen.getByText('block-analytics-vendor')).toBeTruthy();
    expect(screen.getByText('applied')).toBeTruthy();
  });

  it('calls out the rule that short-circuited the flow', async () => {
    const f = flow();
    f.provenance!.short_circuited_by = 'block-vendors:2' as never;
    f.provenance!.entries = [
      {
        seq: 0,
        phase: 'request_short_circuit',
        module: 'm',
        rule_id: 'block-vendors:2',
        rule_name: 'r',
        action: 'block',
        outcome: 'applied',
        duration_ms: 0,
        detail: {},
      },
    ] as never;
    render(<PanelView api={apiWith([f])} tabId={7} pollMs={999_999} />);
    await waitFor(() => expect(screen.getByText('cdn.example.com')).toBeTruthy());
    await userEvent.click(screen.getByText('cdn.example.com'));
    expect(screen.getByText(/short-circuited the flow/)).toBeTruthy();
  });

  it('says plainly when nothing matched', async () => {
    render(<PanelView api={apiWith([flow()])} tabId={7} pollMs={999_999} />);
    await waitFor(() => expect(screen.getByText('cdn.example.com')).toBeTruthy());
    await userEvent.click(screen.getByText('cdn.example.com'));
    expect(screen.getByText(/No rule matched this flow/)).toBeTruthy();
  });

  describe('filter chips', () => {
    const mixed = () => [
      flow({ flow_id: 'plain' }),
      flow({ flow_id: 'mod', modified: true, request: { ...flow().request, host: 'mod.test' } }),
      flow({ flow_id: 'blk', blocked: true, request: { ...flow().request, host: 'blk.test' } }),
    ];

    it('filters to modified', async () => {
      render(<PanelView api={apiWith(mixed())} tabId={7} pollMs={999_999} />);
      await waitFor(() => expect(screen.getByText('mod.test')).toBeTruthy());
      await userEvent.click(screen.getByRole('button', { name: 'modified' }));
      expect(screen.getByText('mod.test')).toBeTruthy();
      expect(screen.queryByText('blk.test')).toBeNull();
    });

    it('filters to blocked', async () => {
      render(<PanelView api={apiWith(mixed())} tabId={7} pollMs={999_999} />);
      await waitFor(() => expect(screen.getByText('blk.test')).toBeTruthy());
      await userEvent.click(screen.getByRole('button', { name: 'blocked' }));
      expect(screen.getByText('blk.test')).toBeTruthy();
      expect(screen.queryByText('mod.test')).toBeNull();
    });

    it('filters to flows carrying warnings', async () => {
      const warned = flow({ flow_id: 'w', request: { ...flow().request, host: 'warn.test' } });
      warned.provenance!.notes = [
        { code: 'csp_modified', severity: 'warning', message: 'x' },
      ] as never;
      render(<PanelView api={apiWith([flow(), warned])} tabId={7} pollMs={999_999} />);
      await waitFor(() => expect(screen.getByText('warn.test')).toBeTruthy());
      await userEvent.click(screen.getByRole('button', { name: 'warnings' }));
      expect(screen.getByText('warn.test')).toBeTruthy();
      expect(screen.queryByText('cdn.example.com')).toBeNull();
    });

    it('offers an unattributed bucket so gaps are visible, not mysterious', async () => {
      const orphan = flow({
        flow_id: 'o',
        tab_id: null,
        request: { ...flow().request, host: 'orphan.test' },
      });
      render(<PanelView api={apiWith([flow(), orphan])} tabId={7} pollMs={999_999} />);
      await waitFor(() => expect(screen.getByText('orphan.test')).toBeTruthy());
      await userEvent.click(screen.getByRole('button', { name: 'unattributed' }));
      expect(screen.getByText('orphan.test')).toBeTruthy();
      expect(screen.queryByText('cdn.example.com')).toBeNull();
    });
  });

  it('links a module rather than embedding an editor', async () => {
    // Authoring belongs in the web UI (SPEC-3 §7.3).
    const onOpenModule = vi.fn();
    const f = flow();
    f.provenance!.evaluated_modules = ['block-vendors'] as never;
    render(<PanelView api={apiWith([f])} tabId={7} pollMs={999_999} onOpenModule={onOpenModule} />);
    await waitFor(() => expect(screen.getByText('cdn.example.com')).toBeTruthy());
    await userEvent.click(screen.getByText('cdn.example.com'));
    await userEvent.click(screen.getByText(/open block-vendors/));
    expect(onOpenModule).toHaveBeenCalledWith('block-vendors');
  });

  it('shows flags on a row', async () => {
    render(
      <PanelView
        api={apiWith([flow({ blocked: true, modified: true })])}
        tabId={7}
        pollMs={999_999}
      />,
    );
    await waitFor(() => expect(screen.getByText('BLK')).toBeTruthy());
    expect(screen.getByText('MOD')).toBeTruthy();
  });
});

describe('redaction rendering (REQ CAP-043)', () => {
  const masked = '«redacted:sha1=a1b2,len=48»';

  function withHeaders(): FlowRecord {
    const base = flow({ flow_id: 'h1', redacted: true });
    return {
      ...base,
      request: {
        ...(base.request as object),
        headers: [
          ['accept', 'text/html'],
          ['cookie', masked],
        ],
      },
      response: {
        ...(base.response as object),
        headers: [['set-cookie', masked]],
      },
    } as unknown as FlowRecord;
  }

  it('shows a masked header as length and fingerprint, never as a value', async () => {
    const api = apiWith([withHeaders()]);
    render(<PanelView api={api} tabId={7} pollMs={0} />);
    await userEvent.click(await screen.findByText('/a.js'));
    // Both the request cookie and the response set-cookie.
    expect(await screen.findAllByText(/48 bytes/)).toHaveLength(2);
    expect(screen.getAllByText(/#a1b2/)).toHaveLength(2);
    // The raw contract string is an implementation detail, not something to
    // put in front of a human.
    expect(screen.queryByText(masked)).toBeNull();
  });

  it('shows unmasked headers normally', async () => {
    const api = apiWith([withHeaders()]);
    render(<PanelView api={api} tabId={7} pollMs={0} />);
    await userEvent.click(await screen.findByText('/a.js'));
    expect(await screen.findByText('text/html')).toBeTruthy();
  });

  it('offers no way to reveal a masked value', async () => {
    // Unmasking is live-ring-only and web-UI-only (SPEC-0 §9.3). If a reveal
    // control ever appears in the panel, this fails — which is the point.
    const api = apiWith([withHeaders()]);
    render(<PanelView api={api} tabId={7} pollMs={0} />);
    await userEvent.click(await screen.findByText('/a.js'));
    await screen.findAllByText(/48 bytes/);
    for (const button of screen.queryAllByRole('button')) {
      expect(button.textContent ?? '').not.toMatch(/reveal|unmask|show value/i);
    }
  });
});
