/** Settings — effective redaction configuration. SPEC-2 §9, REQ CAP-044. */
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { SettingsView } from './SettingsView';
import { ApiClient } from '../../api/client';
import type { DaemonConfig } from '../../api/types';

afterEach(() => vi.restoreAllMocks());

function config(overrides: Partial<DaemonConfig['redaction']> = {}): DaemonConfig {
  return {
    redaction: {
      enabled: true,
      header_patterns: [
        'cookie',
        'set-cookie',
        'authorization',
        'proxy-authorization',
        'x-api-key',
        'x-auth-token',
      ],
      json_key_patterns: ['password', 'token', 'secret'],
      ...overrides,
    },
    capture: { ring_max_flows: 5000 },
  };
}

function api(payload: DaemonConfig = config()): ApiClient {
  const client = new ApiClient('http://127.0.0.1:8081');
  vi.spyOn(client, 'getConfig').mockResolvedValue(payload);
  // Stubbed so this file exercises the redaction form without reaching for a
  // real daemon on behalf of the exclusion list beside it.
  vi.spyOn(client, 'getExclusions').mockResolvedValue({ entries: [] });
  vi.spyOn(client, 'putConfig').mockImplementation((sections) =>
    Promise.resolve({ ...payload, ...(sections as Partial<DaemonConfig>) } as DaemonConfig),
  );
  return client;
}

describe('SettingsView  # REQ CAP-044', () => {
  it('shows the patterns actually in effect, not just the ones typed', async () => {
    // `GET /config` returns the effective configuration, defaults included —
    // otherwise "redaction is configurable" is a claim nobody can check.
    render(<SettingsView api={api()} />);
    await screen.findByText('Redaction');
    expect(screen.getByText('cookie')).toBeTruthy();
    expect(screen.getByText('proxy-authorization')).toBeTruthy();
    const header = screen.getByText('cookie').closest('li')!;
    expect(within(header).getByText('default')).toBeTruthy();
  });

  it('marks a non-default pattern as added', async () => {
    render(<SettingsView api={api(config({ header_patterns: ['cookie', 'x-house-token'] }))} />);
    const added = (await screen.findByText('x-house-token')).closest('li')!;
    expect(within(added).getByText('added')).toBeTruthy();
  });

  it('warns when a default pattern has been removed', async () => {
    render(<SettingsView api={api(config({ header_patterns: ['cookie'] }))} />);
    const fieldset = (await screen.findByText('Header patterns')).closest('fieldset')!;
    expect(within(fieldset).getByText(/Removed from the defaults/).textContent).toContain(
      'authorization',
    );
  });

  it('shows an example of the mask format', async () => {
    render(<SettingsView api={api()} />);
    expect(await screen.findByText('«redacted:sha1=3f9a,len=182»')).toBeTruthy();
  });

  it('adds and removes a header pattern and writes only the redaction section', async () => {
    const client = api();
    render(<SettingsView api={client} />);
    await screen.findByText('Redaction');

    await userEvent.type(screen.getByLabelText('Add a pattern to Header patterns'), 'X-House-Key');
    await userEvent.click(
      within(screen.getByText('Header patterns').closest('fieldset')!).getByRole('button', {
        name: 'Add',
      }),
    );
    await userEvent.click(
      screen.getByRole('button', { name: 'Remove cookie from Header patterns' }),
    );
    await userEvent.click(screen.getByRole('button', { name: 'Save redaction settings' }));

    const sent = vi.mocked(client.putConfig).mock.calls[0]?.[0] as {
      redaction: { header_patterns: string[] };
    };
    expect(Object.keys(sent)).toEqual(['redaction']);
    expect(sent.redaction.header_patterns).toContain('x-house-key');
    expect(sent.redaction.header_patterns).not.toContain('cookie');
  });

  it('warns loudly when redaction is switched off', async () => {
    render(<SettingsView api={api()} />);
    await userEvent.click(await screen.findByLabelText('Redaction enabled'));
    expect(screen.getByText(/written to session files in clear text/)).toBeTruthy();
  });

  it('confirms a save', async () => {
    render(<SettingsView api={api()} />);
    await screen.findByText('Redaction');
    await userEvent.click(screen.getByRole('button', { name: 'Save redaction settings' }));
    expect((await screen.findByRole('status')).textContent).toContain('takes effect immediately');
  });

  it('reports a read failure instead of an empty form', async () => {
    const client = api();
    vi.spyOn(client, 'getConfig').mockRejectedValue(new Error('config.yaml is unreadable'));
    render(<SettingsView api={client} />);
    expect((await screen.findByRole('alert')).textContent).toContain('config.yaml is unreadable');
  });

  it('reports a save failure', async () => {
    const client = api();
    vi.spyOn(client, 'putConfig').mockRejectedValue(new Error('unknown configuration key'));
    render(<SettingsView api={client} />);
    await screen.findByText('Redaction');
    await userEvent.click(screen.getByRole('button', { name: 'Save redaction settings' }));
    expect((await screen.findByRole('alert')).textContent).toContain('unknown configuration key');
  });

  it('ignores an empty or duplicate pattern', async () => {
    const client = api();
    render(<SettingsView api={client} />);
    await screen.findByText('Redaction');
    const fieldset = screen.getByText('Header patterns').closest('fieldset')!;
    const add = within(fieldset).getByRole('button', { name: 'Add' });
    await userEvent.click(add);
    await userEvent.type(within(fieldset).getByLabelText(/Add a pattern/), 'cookie');
    await userEvent.click(add);
    expect(within(fieldset).getAllByText('cookie')).toHaveLength(1);
  });
});
