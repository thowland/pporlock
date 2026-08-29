import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ModuleLibrary } from './ModuleLibrary';
import { ApiClient } from '../../api/client';
import type { ModuleStatus } from '../../api/types';

afterEach(() => vi.restoreAllMocks());

function makeModule(overrides: Partial<ModuleStatus> = {}): ModuleStatus {
  return {
    name: 'block-vendors',
    version: '1.2.0',
    enabled: true,
    priority: 100,
    state: 'loaded',
    has_python: true,
    has_report: false,
    rule_count: 12,
    error: null,
    quarantine: null,
    stats: { flows_matched: 8123, flows_modified: 190, errors: 0, avg_ms: 0.4 },
    ...overrides,
  };
}

function api(modules: ModuleStatus[]): ApiClient {
  const client = new ApiClient('http://127.0.0.1:8081');
  vi.spyOn(client, 'listModules').mockResolvedValue(modules);
  vi.spyOn(client, 'patchModule').mockImplementation((name) =>
    Promise.resolve(makeModule({ name })),
  );
  vi.spyOn(client, 'reloadModules').mockResolvedValue({
    loaded: 1,
    enabled: 1,
    quarantined: 0,
    errors: [],
  });
  return client;
}

describe('ModuleLibrary  # REQ WUI-005', () => {
  it('lists modules in run order with state, priority and stats', async () => {
    const client = api([
      makeModule({ name: 'late', priority: 200 }),
      makeModule({ name: 'early', priority: 10 }),
    ]);
    render(<ModuleLibrary api={client} onOpen={vi.fn()} />);

    await waitFor(() => expect(screen.getByText('early')).toBeTruthy());
    const names = screen
      .getAllByRole('row')
      .slice(1)
      .map((row) => row.textContent);
    expect(names[0]).toContain('early');
    expect(names[1]).toContain('late');
    expect(screen.getAllByText('loaded').length).toBe(2);
  });

  // The whole point of this page: a module that failed to load is visible
  // without a click, traceback and all.
  it('renders a load error inline and expanded with its traceback', async () => {
    const client = api([
      makeModule({
        state: 'load_error',
        enabled: false,
        error: {
          code: 'module_load_failed',
          message: 'invalid syntax',
          trace: 'Traceback (most recent call last):\n  File "module.py", line 3',
          line: 3,
        },
      }),
    ]);
    render(<ModuleLibrary api={client} onOpen={vi.fn()} />);

    await waitFor(() => expect(screen.getByText('module_load_failed')).toBeTruthy());
    const alert = screen.getByRole('alert');
    expect(alert.textContent).toContain('invalid syntax');
    expect(alert.textContent).toContain('Traceback (most recent call last):');
    expect(alert.textContent).toContain('module.yaml line 3');
    expect(screen.getByText('load error')).toBeTruthy();
  });

  it('renders the quarantine reason inline  # REQ MOD-025', async () => {
    const client = api([
      makeModule({
        state: 'quarantined',
        quarantine: { reason: 'raised on 10 consecutive flows', failures: 10, since: '10:02:11' },
      }),
    ]);
    render(<ModuleLibrary api={client} onOpen={vi.fn()} />);
    await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy());
    expect(screen.getByRole('alert').textContent).toContain('raised on 10 consecutive flows');
    expect(screen.getByRole('alert').textContent).toContain('10 consecutive failures');
  });

  it('enables and disables a module through PATCH', async () => {
    const client = api([makeModule()]);
    render(<ModuleLibrary api={client} onOpen={vi.fn()} />);
    const toggle = await screen.findByLabelText('Enable block-vendors');
    await userEvent.click(toggle);
    expect(client.patchModule).toHaveBeenCalledWith('block-vendors', { enabled: false });
  });

  it('reorders by writing the neighbour priority back  # REQ WUI-005', async () => {
    const client = api([
      makeModule({ name: 'early', priority: 10 }),
      makeModule({ name: 'late', priority: 200 }),
    ]);
    render(<ModuleLibrary api={client} onOpen={vi.fn()} />);
    await userEvent.click(await screen.findByLabelText('Move late earlier'));
    expect(client.patchModule).toHaveBeenCalledWith('late', { priority: 10 });
  });

  it('breaks a priority tie rather than writing an identical number', async () => {
    const client = api([
      makeModule({ name: 'a', priority: 100 }),
      makeModule({ name: 'b', priority: 100 }),
    ]);
    render(<ModuleLibrary api={client} onOpen={vi.fn()} />);
    await userEvent.click(await screen.findByLabelText('Move a later'));
    expect(client.patchModule).toHaveBeenCalledWith('a', { priority: 101 });
  });

  it('edits priority directly, committing once on Enter rather than per keystroke', async () => {
    const client = api([makeModule()]);
    render(<ModuleLibrary api={client} onOpen={vi.fn()} />);
    const field = await screen.findByLabelText('Priority for block-vendors');
    await userEvent.clear(field);
    await userEvent.type(field, '5');
    expect(client.patchModule).not.toHaveBeenCalled();
    await userEvent.type(field, '{Enter}');
    expect(client.patchModule).toHaveBeenCalledTimes(1);
    expect(client.patchModule).toHaveBeenCalledWith('block-vendors', { priority: 5 });
  });

  it('reverts a priority field left blank instead of sending zero', async () => {
    const client = api([makeModule()]);
    render(<ModuleLibrary api={client} onOpen={vi.fn()} />);
    const field = await screen.findByLabelText('Priority for block-vendors');
    await userEvent.clear(field);
    await userEvent.tab();
    expect(client.patchModule).not.toHaveBeenCalled();
    expect(field).toHaveProperty('value', '100');
  });

  it('opens the editor for a module', async () => {
    const onOpen = vi.fn();
    render(<ModuleLibrary api={api([makeModule()])} onOpen={onOpen} />);
    await userEvent.click(await screen.findByRole('button', { name: 'block-vendors' }));
    expect(onOpen).toHaveBeenCalledWith('block-vendors');
  });

  it('reloads all modules on request  # REQ MOD-004', async () => {
    const client = api([makeModule()]);
    render(<ModuleLibrary api={client} onOpen={vi.fn()} />);
    await userEvent.click(await screen.findByRole('button', { name: 'Reload all' }));
    await waitFor(() => expect(client.reloadModules).toHaveBeenCalled());
  });

  it('refreshes on request', async () => {
    const client = api([makeModule()]);
    render(<ModuleLibrary api={client} onOpen={vi.fn()} />);
    await userEvent.click(await screen.findByRole('button', { name: 'Refresh' }));
    await waitFor(() => expect(client.listModules).toHaveBeenCalledTimes(2));
  });

  it('says so when the list cannot be read, rather than looking empty', async () => {
    const client = new ApiClient('http://127.0.0.1:8081');
    vi.spyOn(client, 'listModules').mockRejectedValue(new Error('daemon unreachable'));
    render(<ModuleLibrary api={client} onOpen={vi.fn()} />);
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('unreachable'));
    expect(screen.getByText('No modules')).toBeTruthy();
  });

  it('surfaces a failed PATCH', async () => {
    const client = api([makeModule()]);
    vi.spyOn(client, 'patchModule').mockRejectedValue(new Error('read-only'));
    render(<ModuleLibrary api={client} onOpen={vi.fn()} />);
    await userEvent.click(await screen.findByLabelText('Enable block-vendors'));
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('read-only'));
  });

  it('shows an empty state when there are no modules at all', async () => {
    render(<ModuleLibrary api={api([])} onOpen={vi.fn()} />);
    await waitFor(() => expect(screen.getByText('No modules')).toBeTruthy());
  });

  it('disables the move controls at the ends of the list', async () => {
    const client = api([makeModule({ name: 'only' })]);
    render(<ModuleLibrary api={client} onOpen={vi.fn()} />);
    const row = (await screen.findByText('only')).closest('tr')!;
    expect(within(row).getByLabelText('Move only earlier')).toHaveProperty('disabled', true);
    expect(within(row).getByLabelText('Move only later')).toHaveProperty('disabled', true);
  });
});

describe('a module the daemon reports without stats', () => {
  it('renders rather than crashing  # contract: stats is optional', () => {
    // The daemon does not send stats yet (REQ PRF-007) and the contract does
    // not require them. Reading through them unconditionally crashed the whole
    // view — "Cannot read properties of undefined (reading 'flows_matched')" —
    // and every test passed, because every fixture supplied stats the real
    // daemon never sends.
    const client = new ApiClient('http://127.0.0.1:8081');
    const bare = { ...makeModule({ name: 'no-stats' }) };
    delete (bare as { stats?: unknown }).stats;
    vi.spyOn(client, 'listModules').mockResolvedValue([bare]);
    render(<ModuleLibrary api={client} onOpen={vi.fn()} />);
    return waitFor(() => expect(screen.getByText('no-stats')).toBeTruthy());
  });

  it('shows an em dash, not a zero  # "no data" is not "zero"', async () => {
    const client = new ApiClient('http://127.0.0.1:8081');
    const bare = { ...makeModule({ name: 'no-stats' }) };
    delete (bare as { stats?: unknown }).stats;
    vi.spyOn(client, 'listModules').mockResolvedValue([bare]);
    render(<ModuleLibrary api={client} onOpen={vi.fn()} />);
    const row = (await screen.findByText('no-stats')).closest('tr')!;
    expect(within(row).getAllByText('—').length).toBeGreaterThan(0);
  });
});

/**
 * Reports are only useful if you can find them, and only work if they are
 * fetched — OI-29, OI-30.
 *
 * The first version linked to `/modules/<name>/report` with a plain anchor.
 * That cannot work: a navigation carries no Authorization header, so every
 * click returned `missing or invalid bearer token`. Replaces the two tests
 * that asserted the anchor and its `target=_blank`, which described a design
 * that could never have worked.
 */
describe('module reports', () => {
  it('offers a report only where one exists', async () => {
    const client = api([
      makeModule({ name: 'gpc-audit', has_report: true }),
      makeModule({ name: 'adblock', has_report: false }),
    ]);
    render(<ModuleLibrary api={client} onOpen={vi.fn()} />);

    // Linking every module to a 404 would make the column noise.
    await screen.findByText('adblock');
    expect(await screen.findAllByRole('button', { name: 'report' })).toHaveLength(1);
  });

  it('fetches the report with the bearer token rather than navigating', async () => {
    // The whole point of OI-30: the token rides in a header, and a header
    // needs a fetch. Putting it in the URL is forbidden outright.
    const client = api([makeModule({ name: 'gpc-audit', has_report: true })]);
    const fetchReport = vi
      .spyOn(client, 'getModuleReport')
      .mockResolvedValue({ contentType: 'text/html', body: '<p>audit</p>' });
    render(<ModuleLibrary api={client} onOpen={vi.fn()} />);

    await userEvent.click(await screen.findByRole('button', { name: 'report' }));

    expect(fetchReport).toHaveBeenCalledWith('gpc-audit');
  });

  it('renders module HTML in a sandboxed frame, never in this document', async () => {
    // The body is module-authored and this page holds the bearer token. No
    // allow-scripts and no allow-same-origin means an opaque origin that
    // cannot reach the page or the control API.
    const client = api([makeModule({ name: 'gpc-audit', has_report: true })]);
    vi.spyOn(client, 'getModuleReport').mockResolvedValue({
      contentType: 'text/html; charset=utf-8',
      body: '<p>audit</p>',
    });
    render(<ModuleLibrary api={client} onOpen={vi.fn()} />);

    await userEvent.click(await screen.findByRole('button', { name: 'report' }));

    const frame = await screen.findByTitle('gpc-audit report');
    expect(frame.getAttribute('sandbox')).toBe('');
    expect(frame.getAttribute('srcdoc')).toContain('<p>audit</p>');
  });

  it('shows non-HTML reports as text', async () => {
    // Rendering CSV or JSON as markup would smuggle HTML past the daemon's
    // content-type allowlist.
    const client = api([makeModule({ name: 'gpc-audit', has_report: true })]);
    vi.spyOn(client, 'getModuleReport').mockResolvedValue({
      contentType: 'text/csv',
      body: 'host,cookie\nx,<b>y</b>',
    });
    render(<ModuleLibrary api={client} onOpen={vi.fn()} />);

    await userEvent.click(await screen.findByRole('button', { name: 'report' }));

    expect(await screen.findByText(/<b>y<\/b>/)).toBeTruthy();
    expect(screen.queryByTitle('gpc-audit report')).toBeNull();
  });

  it('reports a failure instead of showing an empty frame', async () => {
    const client = api([makeModule({ name: 'gpc-audit', has_report: true })]);
    vi.spyOn(client, 'getModuleReport').mockRejectedValue(new Error('module raised'));
    render(<ModuleLibrary api={client} onOpen={vi.fn()} />);

    await userEvent.click(await screen.findByRole('button', { name: 'report' }));

    expect(await screen.findByText('module raised')).toBeTruthy();
  });
});
