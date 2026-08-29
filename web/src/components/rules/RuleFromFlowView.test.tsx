import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { RuleFromFlowView, newModuleYaml } from './RuleFromFlowView';
import { ApiClient } from '../../api/client';
import { readManifest, readRules } from '../../lib/module-yaml';
import { FIXTURE_MODULE_YAML } from '../../test/module-fixture';
import type { ModuleDetail, Rule } from '../../api/types';

afterEach(() => vi.restoreAllMocks());

const RULE: Rule = {
  name: 'block-cdn-example-com',
  match: { host: 'cdn.example.com', path: '^/a/analytics\\.js$', method: 'GET' },
  action: 'block',
  mode: 'stub',
};

function detail(): ModuleDetail {
  return {
    name: 'block-vendors',
    version: '1.2.0',
    enabled: true,
    priority: 100,
    state: 'loaded',
    has_python: false,
    has_report: false,
    rule_count: 5,
    error: null,
    quarantine: null,
    stats: { flows_matched: 0, flows_modified: 0, errors: 0, avg_ms: 0 },
    files: { 'module.yaml': FIXTURE_MODULE_YAML },
  };
}

function api(): ApiClient {
  const client = new ApiClient('http://127.0.0.1:8081');
  vi.spyOn(client, 'listModules').mockResolvedValue([
    {
      name: 'block-vendors',
      version: '1.2.0',
      enabled: true,
      priority: 100,
      state: 'loaded',
      has_python: false,
      has_report: false,
      rule_count: 5,
      error: null,
      quarantine: null,
      stats: { flows_matched: 0, flows_modified: 0, errors: 0, avg_ms: 0 },
    },
  ]);
  vi.spyOn(client, 'getModule').mockResolvedValue(detail());
  vi.spyOn(client, 'createModule').mockResolvedValue(detail());
  vi.spyOn(client, 'replaceModule').mockResolvedValue(detail());
  return client;
}

describe('newModuleYaml  # REQ MCP-030', () => {
  it('creates the manifest disabled, carrying the one rule', () => {
    const text = newModuleYaml('from-flow', RULE);
    const manifest = readManifest(text)!;
    expect(manifest.enabled).toBe(false);
    expect(manifest.pporlock_api).toBe('1');
    expect(readRules(text)).toEqual([RULE]);
  });
});

describe('RuleFromFlowView  # REQ WUI-008', () => {
  it('opens with the rule already populated in the builder', async () => {
    render(<RuleFromFlowView api={api()} rule={RULE} onCreated={vi.fn()} onCancel={vi.fn()} />);
    expect(screen.getByLabelText('Rule name')).toHaveProperty('value', 'block-cdn-example-com');
    expect(screen.getByLabelText('Host')).toHaveProperty('value', 'cdn.example.com');
    expect(screen.getByLabelText('Path')).toHaveProperty('value', '^/a/analytics\\.js$');
    expect(screen.getByLabelText('Method')).toHaveProperty('value', 'GET');
  });

  it('states that a new module is created disabled', () => {
    render(<RuleFromFlowView api={api()} rule={RULE} onCreated={vi.fn()} onCancel={vi.fn()} />);
    expect(screen.getByRole('note').textContent).toContain('created disabled');
  });

  it('creates a new module holding the rule', async () => {
    const client = api();
    const onCreated = vi.fn();
    render(<RuleFromFlowView api={client} rule={RULE} onCreated={onCreated} onCancel={vi.fn()} />);
    await userEvent.click(screen.getByRole('button', { name: 'Create module (disabled)' }));
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith('block-cdn-example-com'));
    const [name, files] = vi.mocked(client.createModule).mock.calls[0]!;
    expect(name).toBe('block-cdn-example-com');
    expect(readManifest(files['module.yaml']!)!.enabled).toBe(false);
    expect(readRules(files['module.yaml']!)).toEqual([RULE]);
  });

  it('appends into an existing module without disturbing its other rules', async () => {
    const client = api();
    const onCreated = vi.fn();
    render(<RuleFromFlowView api={client} rule={RULE} onCreated={onCreated} onCancel={vi.fn()} />);
    await userEvent.selectOptions(
      await screen.findByLabelText('Destination module'),
      'block-vendors',
    );
    await userEvent.click(screen.getByRole('button', { name: 'Add to block-vendors' }));

    await waitFor(() => expect(onCreated).toHaveBeenCalledWith('block-vendors'));
    const [, files] = vi.mocked(client.replaceModule).mock.calls[0]!;
    const rules = readRules(files['module.yaml']!);
    expect(rules).toHaveLength(6);
    expect(rules[5]).toEqual(RULE);
    expect(rules.slice(0, 5)).toEqual(readRules(FIXTURE_MODULE_YAML));
    expect(files['module.yaml']).toContain('# First match wins');
  });

  it('renames a new module to a slug the daemon will accept', async () => {
    const client = api();
    render(<RuleFromFlowView api={client} rule={RULE} onCreated={vi.fn()} onCancel={vi.fn()} />);
    const field = screen.getByLabelText('New module name');
    await userEvent.clear(field);
    await userEvent.type(field, 'My New Module');
    await userEvent.click(screen.getByRole('button', { name: 'Create module (disabled)' }));
    await waitFor(() => expect(client.createModule).toHaveBeenCalled());
    expect(vi.mocked(client.createModule).mock.calls[0]![0]).toBe('my-new-module');
  });

  it('refuses a nameless rule', async () => {
    const client = api();
    render(<RuleFromFlowView api={client} rule={RULE} onCreated={vi.fn()} onCancel={vi.fn()} />);
    await userEvent.clear(screen.getByLabelText('Rule name'));
    await userEvent.click(screen.getByRole('button', { name: 'Create module (disabled)' }));
    expect(screen.getByRole('alert').textContent).toContain('needs a name');
    expect(client.createModule).not.toHaveBeenCalled();
  });

  it('surfaces a failed create', async () => {
    const client = api();
    vi.spyOn(client, 'createModule').mockRejectedValue(new Error('name already taken'));
    render(<RuleFromFlowView api={client} rule={RULE} onCreated={vi.fn()} onCancel={vi.fn()} />);
    await userEvent.click(screen.getByRole('button', { name: 'Create module (disabled)' }));
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('already taken'));
  });

  it('still offers a new module when the library cannot be listed', async () => {
    const client = api();
    vi.spyOn(client, 'listModules').mockRejectedValue(new Error('offline'));
    render(<RuleFromFlowView api={client} rule={RULE} onCreated={vi.fn()} onCancel={vi.fn()} />);
    await waitFor(() =>
      expect(screen.getByLabelText('Destination module').children).toHaveLength(1),
    );
  });

  it('goes back to the traffic view on cancel', async () => {
    const onCancel = vi.fn();
    render(<RuleFromFlowView api={api()} rule={RULE} onCreated={vi.fn()} onCancel={onCancel} />);
    await userEvent.click(screen.getByRole('button', { name: '← Traffic' }));
    expect(onCancel).toHaveBeenCalled();
  });
});
