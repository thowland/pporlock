/** Dry run. SPEC-2 §8.3, REQ WUI-010, CAP-030, CAP-032, CAP-033. */
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { DryRunView } from './DryRunView';
import { ApiClient } from '../../api/client';
import { makeDryRun, makeProvenance, makeSession } from '../../test/factories';
import type { DryRunResult, ModuleStatus } from '../../api/types';

afterEach(() => vi.restoreAllMocks());

const MODULE: ModuleStatus = {
  name: 'strip-sri',
  version: '1.0.0',
  enabled: false,
  priority: 100,
  state: 'disabled',
  has_python: true,
  has_report: false,
  rule_count: 2,
  error: null,
  quarantine: null,
  stats: { flows_matched: 0, flows_modified: 0, errors: 0, avg_ms: 0 },
};

const WITH_RESULTS: DryRunResult = makeDryRun({
  summary: {
    flows_evaluated: 500,
    matched: 2,
    modified: 1,
    blocked: 1,
    errors: 0,
    avg_ms: 0.9,
    p95_ms: 3.1,
  },
  results: [
    {
      flow_id: 'f1',
      url: 'https://cdn.example.com/a.js',
      provenance: makeProvenance({
        entries: [
          {
            seq: 0,
            phase: 'response_headers',
            module: 'strip-sri',
            rule_id: 'r1',
            rule_name: 'drop csp',
            action: 'headers',
            outcome: 'applied',
            duration_ms: 0.4,
            detail: {},
          },
        ],
      } as Parameters<typeof makeProvenance>[0]),
      diff: {
        headers: [{ op: 'remove', name: 'content-security-policy' }],
        body: { kind: 'unified', text: '@@ -1,4 +1,4 @@\n-old\n+new', truncated: false },
      },
    },
  ],
});

function api(result: DryRunResult = WITH_RESULTS): ApiClient {
  const client = new ApiClient('http://127.0.0.1:8081');
  vi.spyOn(client, 'getSession').mockResolvedValue(makeSession());
  vi.spyOn(client, 'listModules').mockResolvedValue([MODULE]);
  vi.spyOn(client, 'dryRun').mockResolvedValue(result);
  return client;
}

function view(client: ApiClient) {
  return <DryRunView api={client} sessionId="s1" onBack={() => {}} />;
}

describe('DryRunView  # REQ CAP-032', () => {
  it('states permanently that the run executes the module Python', async () => {
    // Not a tooltip and not behind a click: for an agent-authored module this
    // is unread code running on the user's machine (REQ CAP-032, MOD-031).
    render(view(api()));
    const warning = await screen.findByRole('note');
    expect(warning.textContent).toContain("executes the module's Python code");
    expect(warning.textContent).toContain('unsandboxed');
    expect(warning.textContent).toContain('AI agent');
  });

  it('names the code execution in the run button itself', async () => {
    render(view(api()));
    expect(await screen.findByRole('button', { name: 'Run and execute module code' })).toBeTruthy();
  });
});

describe('DryRunView  # REQ CAP-033', () => {
  it('renders the aggregate summary', async () => {
    const client = api();
    render(view(client));
    await userEvent.click(await screen.findByRole('button', { name: /Run and execute/ }));
    const summary = await screen.findByLabelText('Dry run summary');
    expect(summary.textContent).toContain('flows evaluated');
    expect(summary.textContent).toContain('500');
    expect(summary.textContent).toContain('3.10ms');
  });

  it('runs against the chosen session and module', async () => {
    const client = api();
    render(view(client));
    await userEvent.click(await screen.findByRole('button', { name: /Run and execute/ }));
    expect(client.dryRun).toHaveBeenCalledWith('s1', {
      use_installed: ['strip-sri'],
      limit: 500,
      include_diffs: true,
    });
  });

  it('shows per-flow provenance and diff using the shared provenance view', async () => {
    render(view(api()));
    await userEvent.click(await screen.findByRole('button', { name: /Run and execute/ }));
    await userEvent.click(await screen.findByRole('button', { name: /cdn\.example\.com/ }));
    // The provenance component the live view uses, not a second rendering.
    expect(screen.getByText('drop csp')).toBeTruthy();
    expect(screen.getByText('Response — headers')).toBeTruthy();
    expect(screen.getByText('content-security-policy')).toBeTruthy();
    expect(screen.getByText(/@@ -1,4 \+1,4 @@/)).toBeTruthy();
  });

  it('collapses unaffected flows but keeps them countable', async () => {
    // "My rule matched nothing" is the most common dry-run outcome, so the
    // count is always visible even though the detail is not (SPEC-2 §8.3).
    render(view(api()));
    await userEvent.click(await screen.findByRole('button', { name: /Run and execute/ }));
    const toggle = await screen.findByRole('button', { name: /498 unaffected flows/ });
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    await userEvent.click(toggle);
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    expect(screen.getByText(/matched no rule in this module/)).toBeTruthy();
  });

  it('says so loudly when nothing was affected at all', async () => {
    render(view(api(makeDryRun({ summary: { ...WITH_RESULTS.summary, matched: 0 } }))));
    await userEvent.click(await screen.findByRole('button', { name: /Run and execute/ }));
    expect(await screen.findByText('Nothing was affected')).toBeTruthy();
  });

  it('passes through the daemon note when the result list was capped', async () => {
    render(
      view(
        api(
          makeDryRun({
            ...WITH_RESULTS,
            results_total: 90,
            results_shown: 1,
            results_note: 'Showing the first 1 of 90 affected flows',
          }),
        ),
      ),
    );
    await userEvent.click(await screen.findByRole('button', { name: /Run and execute/ }));
    expect(await screen.findByText(/Showing the first 1 of 90/)).toBeTruthy();
  });

  it('refuses to run without a module chosen', async () => {
    const client = api();
    vi.spyOn(client, 'listModules').mockResolvedValue([]);
    render(view(client));
    await userEvent.click(await screen.findByRole('button', { name: /Run and execute/ }));
    expect(screen.getByRole('alert').textContent).toContain('Choose a module');
    expect(client.dryRun).not.toHaveBeenCalled();
  });

  it('reports a failed run', async () => {
    const client = api();
    vi.spyOn(client, 'dryRun').mockRejectedValue(new Error('module raised on import'));
    render(view(client));
    await userEvent.click(await screen.findByRole('button', { name: /Run and execute/ }));
    expect((await screen.findByRole('alert')).textContent).toContain('module raised on import');
  });

  it('honours a limit and the diff toggle', async () => {
    const client = api();
    render(view(client));
    await screen.findByRole('button', { name: /Run and execute/ });
    await userEvent.clear(screen.getByLabelText('Flows'));
    await userEvent.type(screen.getByLabelText('Flows'), '25');
    await userEvent.click(screen.getByLabelText('Include diffs'));
    await userEvent.click(screen.getByRole('button', { name: /Run and execute/ }));
    expect(client.dryRun).toHaveBeenCalledWith('s1', {
      use_installed: ['strip-sri'],
      limit: 25,
      include_diffs: false,
    });
  });

  it('pre-selects the module the editor sent it to', async () => {
    const client = api();
    render(<DryRunView api={client} sessionId="s1" onBack={() => {}} initialModule="strip-sri" />);
    await waitFor(() =>
      expect((screen.getByLabelText('Module') as HTMLSelectElement).value).toBe('strip-sri'),
    );
  });
});
