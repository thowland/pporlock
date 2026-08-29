/**
 * The settings form, rendered from a declaration rather than from anything it
 * knows about a particular module.
 *
 * The tests that matter here are the two ways this could be quietly wrong:
 * sending values the user never chose (which freezes today's defaults into
 * their state forever), and half-applying a form the daemon refused.
 */
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiClient } from '../../api/client';
import type { ModuleDetail, ModuleSetting } from '../../api/types';
import { ModuleSettings, changedFrom } from './ModuleSettings';

afterEach(() => vi.restoreAllMocks());

const SETTINGS: ModuleSetting[] = [
  {
    key: 'identity',
    type: 'enum',
    label: 'Identify as',
    description: 'Which crawler to present.',
    default: 'googlebot',
    options: [
      { value: 'googlebot', label: 'Googlebot' },
      { value: 'claudebot', label: 'ClaudeBot' },
    ],
  },
  { key: 'hosts', type: 'string_list', label: 'Hosts', default: ['*'] },
  { key: 'strip_client_hints', type: 'boolean', label: 'Remove client hints', default: true },
  { key: 'repeats', type: 'integer', label: 'Repeats', default: 2, min: 1, max: 9 },
  { key: 'custom', type: 'text', label: 'Custom user agent', default: '' },
];

function detail(overrides: Partial<ModuleDetail> = {}): ModuleDetail {
  return {
    name: 'user-agent-switcher',
    version: '1.0.0',
    enabled: false,
    priority: 85,
    state: 'loaded',
    has_python: true,
    has_report: true,
    has_settings: true,
    rule_count: 0,
    error: null,
    quarantine: null,
    files: {},
    settings: SETTINGS,
    config: {
      identity: 'googlebot',
      hosts: ['*'],
      strip_client_hints: true,
      repeats: 2,
      custom: '',
    },
    ...overrides,
  };
}

function api(module: ModuleDetail = detail()): ApiClient {
  const client = new ApiClient('http://127.0.0.1:8081');
  vi.spyOn(client, 'getModule').mockResolvedValue(module);
  vi.spyOn(client, 'patchModule').mockResolvedValue(module);
  return client;
}

function open(client: ApiClient, onSaved = vi.fn()) {
  render(
    <ModuleSettings api={client} name="user-agent-switcher" onClose={vi.fn()} onSaved={onSaved} />,
  );
}

describe('changedFrom', () => {
  it('sends nothing when nothing was changed', () => {
    /*
     * Sending everything would work and would be wrong: it freezes today's
     * defaults into the user's state, so a later version of the module that
     * improved one would never reach anyone who had opened this dialog once.
     */
    expect(changedFrom(SETTINGS, { identity: 'googlebot', hosts: ['*'] })).toEqual({});
  });

  it('sends only the fields that differ', () => {
    expect(changedFrom(SETTINGS, { identity: 'claudebot', hosts: ['*'] })).toEqual({
      identity: 'claudebot',
    });
  });

  it('compares a list by its contents, not by identity', () => {
    expect(changedFrom(SETTINGS, { hosts: ['*'] })).toEqual({});
    expect(changedFrom(SETTINGS, { hosts: ['a.com'] })).toEqual({ hosts: ['a.com'] });
  });
});

describe('the settings form', () => {
  it('renders one control per declared field, from the declaration', () => {
    // This component knows six field types and nothing about what any module
    // does with them.
    open(api());
    return screen.findByLabelText('Identify as').then(async () => {
      expect(await screen.findByLabelText('Hosts')).toBeTruthy();
      expect(await screen.findByLabelText('Remove client hints')).toBeTruthy();
      expect(await screen.findByLabelText('Repeats')).toBeTruthy();
    });
  });

  it('shows the value in force, not the declared default', async () => {
    const client = api(detail({ config: { identity: 'claudebot', hosts: ['a.com'] } }));
    open(client);
    const select = (await screen.findByLabelText('Identify as')) as HTMLSelectElement;
    expect(select.value).toBe('claudebot');
  });

  it('sends only what the user touched', async () => {
    const client = api();
    const patch = vi.spyOn(client, 'patchModule').mockResolvedValue(detail());
    open(client);

    await userEvent.selectOptions(await screen.findByLabelText('Identify as'), 'claudebot');
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));

    expect(patch).toHaveBeenCalledWith('user-agent-switcher', {
      config: { identity: 'claudebot' },
    });
  });

  it('sends nothing until Save', async () => {
    /*
     * A PATCH per keystroke would send whatever a half-typed field held to a
     * module that is modifying live traffic.
     */
    const client = api();
    const patch = vi.spyOn(client, 'patchModule').mockResolvedValue(detail());
    open(client);

    await userEvent.selectOptions(await screen.findByLabelText('Identify as'), 'claudebot');
    await userEvent.type(screen.getByLabelText('Custom user agent'), 'MyBot');

    expect(patch).not.toHaveBeenCalled();
  });

  it('resets by sending an empty override map, not by sending the defaults', async () => {
    // Omitting a key is how "put this back to its default" is expressed; the
    // daemon replaces the override map wholesale.
    const client = api(detail({ config: { identity: 'claudebot', hosts: ['a.com'] } }));
    const patch = vi.spyOn(client, 'patchModule').mockResolvedValue(detail());
    open(client);

    await screen.findByLabelText('Identify as');
    await userEvent.click(screen.getByRole('button', { name: 'Reset to defaults' }));
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));

    expect(patch).toHaveBeenCalledWith('user-agent-switcher', { config: {} });
  });

  it('splits a list field on newlines', async () => {
    // One per line rather than comma-separated: host globs and header names
    // may contain a comma, and a separator that appears in the data is not one.
    const client = api();
    const patch = vi.spyOn(client, 'patchModule').mockResolvedValue(detail());
    open(client);

    const hosts = await screen.findByLabelText('Hosts');
    await userEvent.clear(hosts);
    await userEvent.type(hosts, 'a.com{enter}b.com');
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));

    expect(patch).toHaveBeenCalledWith('user-agent-switcher', {
      config: { hosts: ['a.com', 'b.com'] },
    });
  });

  it('keeps the form open and shows why when the daemon refuses', async () => {
    // The daemon writes nothing when it refuses, so closing would discard
    // edits the user still has the only copy of.
    const client = api();
    vi.spyOn(client, 'patchModule').mockRejectedValue(new Error('identity: expected one of a, b'));
    const onClose = vi.fn();
    render(
      <ModuleSettings
        api={client}
        name="user-agent-switcher"
        onClose={onClose}
        onSaved={vi.fn()}
      />,
    );

    await userEvent.selectOptions(await screen.findByLabelText('Identify as'), 'claudebot');
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));

    expect(await screen.findByText(/expected one of/)).toBeTruthy();
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByLabelText('Identify as')).toBeTruthy();
  });

  it('tells the library to refresh after a successful save', async () => {
    // A settings change alters what the module does on the next flow, and a
    // table still showing the old state is how you debug a change you made.
    const client = api();
    const onSaved = vi.fn();
    open(client, onSaved);

    await userEvent.selectOptions(await screen.findByLabelText('Identify as'), 'claudebot');
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));

    expect(onSaved).toHaveBeenCalled();
  });

  it('says that settings feed unsandboxed module code  # REQ MOD-031', async () => {
    // Every authoring surface has to say so, and a surface that changes what
    // module code does is one of them.
    open(api());
    expect(await screen.findByText(/unsandboxed/)).toBeTruthy();
  });

  it('says so rather than showing an empty form when a module declares nothing', async () => {
    open(api(detail({ settings: [], has_settings: false })));
    expect(await screen.findByText(/declares no settings/)).toBeTruthy();
  });
});
