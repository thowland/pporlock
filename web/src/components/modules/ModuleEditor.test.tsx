import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ModuleEditor } from './ModuleEditor';
import { ApiClient } from '../../api/client';
import type { ModuleDetail } from '../../api/types';
import { FIXTURE_MODULE_YAML } from '../../test/module-fixture';
import type { CodeEditorProps } from '../editor/types';
import { PlainEditor } from '../editor/PlainEditor';

afterEach(() => vi.restoreAllMocks());

/**
 * Monaco cannot mount under jsdom, so every test here loads the plain editor
 * through the same `load` seam the application uses. The marker *contract* is
 * what these tests assert; marker placement inside Monaco is covered by e2e.
 */
const loadPlain = () => Promise.resolve<React.ComponentType<CodeEditorProps>>(PlainEditor);

function detail(overrides: Partial<ModuleDetail> = {}): ModuleDetail {
  return {
    name: 'block-vendors',
    version: '1.2.0',
    enabled: false,
    priority: 100,
    state: 'loaded',
    has_python: true,
    has_report: false,
    has_settings: false,
    rule_count: 5,
    error: null,
    quarantine: null,
    stats: { flows_matched: 0, flows_modified: 0, errors: 0, avg_ms: 0 },
    files: {
      'module.yaml': FIXTURE_MODULE_YAML,
      'module.py': 'def on_request(flow, ctx):\n    pass\n',
    },
    ...overrides,
  };
}

function api(over: Partial<ModuleDetail> = {}): ApiClient {
  const client = new ApiClient('http://127.0.0.1:8081');
  vi.spyOn(client, 'getModule').mockResolvedValue(detail(over));
  vi.spyOn(client, 'replaceModule').mockResolvedValue(detail(over));
  vi.spyOn(client, 'reloadModules').mockResolvedValue({
    loaded: 3,
    enabled: 2,
    quarantined: 0,
    errors: [],
  });
  vi.spyOn(client, 'validateModule').mockResolvedValue({ ok: true, errors: [] });
  return client;
}

function mount(client: ApiClient) {
  return render(<ModuleEditor api={client} name="block-vendors" editorLoad={loadPlain} />);
}

describe('ModuleEditor  # REQ WUI-006', () => {
  it('loads both files into a tab pair', async () => {
    const client = api();
    mount(client);
    await waitFor(() =>
      expect(screen.getByLabelText('module.yaml editor')).toHaveProperty(
        'value',
        FIXTURE_MODULE_YAML,
      ),
    );
    await userEvent.click(screen.getByRole('tab', { name: 'module.py' }));
    expect(screen.getByLabelText('module.py editor')).toHaveProperty(
      'value',
      'def on_request(flow, ctx):\n    pass\n',
    );
  });

  it('saves and reloads in one action, surfacing the reload result', async () => {
    const client = api();
    mount(client);
    await screen.findByLabelText('module.yaml editor');
    await userEvent.click(screen.getByRole('button', { name: 'Save and reload' }));
    await waitFor(() =>
      expect(screen.getByRole('status').textContent).toContain(
        'Saved and reloaded. 3 loaded, 2 enabled, 0 quarantined.',
      ),
    );
    expect(client.replaceModule).toHaveBeenCalledWith('block-vendors', {
      'module.yaml': FIXTURE_MODULE_YAML,
      'module.py': 'def on_request(flow, ctx):\n    pass\n',
    });
    expect(client.reloadModules).toHaveBeenCalled();
  });

  it('reports a reload that failed rather than claiming success', async () => {
    const client = api();
    vi.spyOn(client, 'reloadModules').mockResolvedValue({
      loaded: 2,
      enabled: 1,
      quarantined: 0,
      errors: [{ code: 'module_load_failed', message: 'invalid syntax' }],
    });
    mount(client);
    await screen.findByLabelText('module.yaml editor');
    await userEvent.click(screen.getByRole('button', { name: 'Save and reload' }));
    await waitFor(() =>
      expect(screen.getByRole('status').textContent).toContain(
        'module_load_failed: invalid syntax',
      ),
    );
  });

  it('saves without reloading when asked', async () => {
    const client = api();
    mount(client);
    await screen.findByLabelText('module.yaml editor');
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() =>
      expect(screen.getByRole('status').textContent).toContain('Modules were not reloaded'),
    );
    expect(client.reloadModules).not.toHaveBeenCalled();
  });

  it('surfaces a save failure', async () => {
    const client = api();
    vi.spyOn(client, 'replaceModule').mockRejectedValue(new Error('disk full'));
    mount(client);
    await screen.findByLabelText('module.yaml editor');
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(screen.getByRole('status').textContent).toContain('disk full'));
  });

  it('reports a module that cannot be opened', async () => {
    const client = new ApiClient('http://127.0.0.1:8081');
    vi.spyOn(client, 'getModule').mockRejectedValue(new Error('not found'));
    mount(client);
    await waitFor(() => expect(screen.getByRole('status').textContent).toContain('not found'));
  });

  it('marks the buffer unsaved once edited', async () => {
    mount(api());
    const area = await screen.findByLabelText('module.yaml editor');
    await userEvent.type(area, '\n');
    expect(screen.getByTitle('Unsaved changes')).toBeTruthy();
  });
});

describe('validation markers  # REQ API-027', () => {
  it('turns validation errors into markers keyed by line and column', async () => {
    const client = api();
    vi.spyOn(client, 'validateModule').mockResolvedValue({
      ok: false,
      errors: [
        { code: 'unknown_key', message: 'unknown top-level key: prioriy', line: 7, column: 3 },
      ],
      warnings: [
        {
          code: 'deprecated',
          message: 'transform renamed',
          file: 'module.py',
          severity: 'warning',
        },
      ],
    });
    mount(client);
    await screen.findByLabelText('module.yaml editor');
    await userEvent.click(screen.getByRole('button', { name: 'Validate' }));

    await waitFor(() => expect(screen.getByLabelText('Validation problems')).toBeTruthy());
    const problems = screen.getByLabelText('Validation problems');
    expect(problems.textContent).toContain('7:3');
    expect(problems.textContent).toContain('unknown top-level key: prioriy');
    expect(screen.getByRole('status').textContent).toContain('1 validation error');
    // The module.py warning belongs to the other tab, not this one.
    expect(problems.textContent).not.toContain('transform renamed');
  });

  it('routes a finding without line information to the top of module.yaml', async () => {
    const client = api();
    vi.spyOn(client, 'validateModule').mockResolvedValue({
      ok: false,
      errors: [{ code: 'schema', message: 'no rules' }],
    });
    mount(client);
    await screen.findByLabelText('module.yaml editor');
    await userEvent.click(screen.getByRole('button', { name: 'Validate' }));
    await waitFor(() =>
      expect(screen.getByLabelText('Validation problems').textContent).toContain('1:1'),
    );
  });

  it('shows python findings on the python tab', async () => {
    const client = api();
    vi.spyOn(client, 'validateModule').mockResolvedValue({
      ok: false,
      errors: [{ code: 'syntax_error', message: 'invalid syntax', file: 'module.py', line: 3 }],
    });
    mount(client);
    await screen.findByLabelText('module.yaml editor');
    await userEvent.click(screen.getByRole('button', { name: 'Validate' }));
    await userEvent.click(screen.getByRole('tab', { name: 'module.py' }));
    await waitFor(() =>
      expect(screen.getByLabelText('Validation problems').textContent).toContain('invalid syntax'),
    );
  });

  it('reports YAML syntax errors locally, without a round trip', async () => {
    const client = api({ files: { 'module.yaml': 'name: ok\nrules: [oops\n' } });
    mount(client);
    await waitFor(() =>
      expect(screen.getByLabelText('Validation problems').textContent).toContain('yaml_syntax'),
    );
    expect(client.validateModule).not.toHaveBeenCalled();
  });

  it('says plainly that validation installs nothing', async () => {
    mount(api());
    await screen.findByLabelText('module.yaml editor');
    await userEvent.click(screen.getByRole('button', { name: 'Validate' }));
    await waitFor(() =>
      expect(screen.getByRole('status').textContent).toContain('Nothing was installed'),
    );
  });

  it('surfaces a validation call that failed', async () => {
    const client = api();
    vi.spyOn(client, 'validateModule').mockRejectedValue(new Error('daemon unreachable'));
    mount(client);
    await screen.findByLabelText('module.yaml editor');
    await userEvent.click(screen.getByRole('button', { name: 'Validate' }));
    await waitFor(() => expect(screen.getByRole('status').textContent).toContain('unreachable'));
  });
});

describe('the rule builder tab  # REQ WUI-007', () => {
  it('writes an edited rule back into module.yaml without touching the others', async () => {
    mount(api());
    await screen.findByLabelText('module.yaml editor');
    await userEvent.click(screen.getByRole('tab', { name: 'rule builder' }));
    await userEvent.selectOptions(screen.getByLabelText('Rule to edit'), 'strip-csp-on-html');

    const host = screen.getByLabelText('Content type');
    await userEvent.clear(host);
    await userEvent.type(host, 'application/xhtml+xml');
    await userEvent.click(screen.getByRole('button', { name: 'Apply to module.yaml' }));

    expect(screen.getByRole('status').textContent).toContain('Wrote "strip-csp-on-html"');
    await userEvent.click(screen.getByRole('tab', { name: 'module.yaml' }));
    const text = (screen.getByLabelText('module.yaml editor') as HTMLTextAreaElement).value;
    expect(text).toContain('application/xhtml+xml');
    expect(text).toContain('# First match wins for short-circuit actions (REQ MOD-012).');
    expect(text).toContain('config:\n  vendor_list: []\n');
  });

  it('reports a no-op rather than dirtying the file  # REQ WUI-007', async () => {
    mount(api());
    await screen.findByLabelText('module.yaml editor');
    await userEvent.click(screen.getByRole('tab', { name: 'rule builder' }));
    await userEvent.selectOptions(screen.getByLabelText('Rule to edit'), 'send-to-fixture');
    await userEvent.click(screen.getByRole('button', { name: 'Apply to module.yaml' }));
    expect(screen.getByRole('status').textContent).toContain('No change');
    expect(screen.queryByTitle('Unsaved changes')).toBeNull();
  });

  it('appends a brand new rule', async () => {
    mount(api());
    await screen.findByLabelText('module.yaml editor');
    await userEvent.click(screen.getByRole('tab', { name: 'rule builder' }));
    await userEvent.type(screen.getByLabelText('Rule name'), 'brand-new');
    await userEvent.type(screen.getByLabelText('Host'), 'new.example');
    await userEvent.click(screen.getByRole('button', { name: 'Apply to module.yaml' }));

    await userEvent.click(screen.getByRole('tab', { name: 'module.yaml' }));
    const text = (screen.getByLabelText('module.yaml editor') as HTMLTextAreaElement).value;
    expect(text).toContain('brand-new');
    expect(text).toContain('new.example');
  });

  it('refuses to write a nameless rule', async () => {
    mount(api());
    await screen.findByLabelText('module.yaml editor');
    await userEvent.click(screen.getByRole('tab', { name: 'rule builder' }));
    await userEvent.click(screen.getByRole('button', { name: 'Apply to module.yaml' }));
    expect(screen.getByRole('status').textContent).toContain('needs a name');
  });

  it('resets to a blank draft when the new-rule option is chosen', async () => {
    mount(api());
    await screen.findByLabelText('module.yaml editor');
    await userEvent.click(screen.getByRole('tab', { name: 'rule builder' }));
    await userEvent.selectOptions(screen.getByLabelText('Rule to edit'), 'send-to-fixture');
    expect(screen.getByLabelText('Rule name')).toHaveProperty('value', 'send-to-fixture');
    await userEvent.selectOptions(screen.getByLabelText('Rule to edit'), '');
    expect(screen.getByLabelText('Rule name')).toHaveProperty('value', '');
  });
});
