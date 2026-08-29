/** One-click "exclude this host" from a flow. SPEC-2 §6.6, REQ PXY-016. */
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ExcludeHostAction } from './ExcludeHostAction';
import { ApiClient } from '../../api/client';
import type { ExclusionEntry } from '../../api/types';

afterEach(() => vi.restoreAllMocks());

const SHIPPED: ExclusionEntry[] = [
  { pattern: '*.apple.com', comment: 'pinning: iCloud clients fail closed', source: 'default' },
  { pattern: 'ocsp.digicert.com', comment: 'update: revocation', source: 'default' },
];

function api(entries: ExclusionEntry[] = SHIPPED): ApiClient {
  const client = new ApiClient('http://127.0.0.1:8081');
  vi.spyOn(client, 'getExclusions').mockResolvedValue({ entries });
  vi.spyOn(client, 'putExclusions').mockImplementation((next) =>
    Promise.resolve({ entries: next }),
  );
  return client;
}

async function openConfirm(host = 'cdn.example.com') {
  await userEvent.click(screen.getByRole('button', { name: `Exclude ${host} from interception` }));
}

describe('ExcludeHostAction  # REQ PXY-016', () => {
  it('is one click from the row to the confirmation — the host is never typed', async () => {
    render(<ExcludeHostAction api={api()} host="cdn.example.com" surface="flow table" />);
    await openConfirm();
    const dialog = screen.getByRole('dialog', { name: 'Exclude cdn.example.com' });
    expect(dialog.textContent).toContain('cdn.example.com');
    expect(screen.queryByRole('textbox')).toBeNull();
  });

  it('states the consequence before it takes effect', async () => {
    // Excluding makes a host visible but not readable, and only for new
    // connections. Both have to be said, or the user learns them by being
    // confused later.
    render(<ExcludeHostAction api={api()} host="cdn.example.com" surface="flow table" />);
    await openConfirm();
    const dialog = screen.getByRole('dialog', { name: 'Exclude cdn.example.com' });
    expect(dialog.textContent).toContain('tunnelled without being decrypted');
    expect(dialog.textContent).toContain('nothing about its content');
    expect(dialog.textContent).toContain('new connections only');
    expect(dialog.textContent).toContain('reload');
  });

  it('does nothing at all until the confirmation is accepted', async () => {
    const client = api();
    render(<ExcludeHostAction api={client} host="cdn.example.com" surface="flow table" />);
    await openConfirm();
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(vi.mocked(client.putExclusions)).not.toHaveBeenCalled();
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('appends to the list it just read rather than replacing it', async () => {
    // PUT /exclusions replaces everything. A PUT built from anything but a
    // fresh GET deletes the shipped entries that keep pinned hosts working.
    const client = api();
    render(<ExcludeHostAction api={client} host="cdn.example.com" surface="flow table" />);
    await openConfirm();
    await userEvent.click(screen.getByRole('button', { name: 'Exclude this host' }));
    await screen.findByRole('status');

    const sent = vi.mocked(client.putExclusions).mock.calls[0]?.[0] as ExclusionEntry[];
    expect(sent.slice(0, 2)).toEqual(SHIPPED);
    expect(sent[2]).toEqual({
      pattern: 'cdn.example.com',
      comment: 'added from the flow table (cdn.example.com)',
      source: 'user',
    });
  });

  it('says what will happen and when, since the flow on screen does not change', async () => {
    render(<ExcludeHostAction api={api()} host="cdn.example.com" surface="flow table" />);
    await openConfirm();
    await userEvent.click(screen.getByRole('button', { name: 'Exclude this host' }));
    const result = await screen.findByRole('status');
    expect(result.textContent).toContain('now excluded');
    expect(result.textContent).toContain('New TLS connections');
    expect(result.textContent).toContain('Reload the page');
  });

  it('answers an already-excluded host instead of adding a duplicate', async () => {
    const client = api();
    render(<ExcludeHostAction api={client} host="gs.apple.com" surface="flow table" />);
    await userEvent.click(
      screen.getByRole('button', { name: 'Exclude gs.apple.com from interception' }),
    );
    await userEvent.click(screen.getByRole('button', { name: 'Exclude this host' }));

    const result = await screen.findByRole('status');
    expect(result.textContent).toContain('already excluded by *.apple.com (default)');
    expect(result.textContent).toContain('Nothing was changed');
    expect(vi.mocked(client.putExclusions)).not.toHaveBeenCalled();
  });

  it('reports a failure rather than implying the host is excluded', async () => {
    const client = api();
    vi.spyOn(client, 'putExclusions').mockRejectedValue(new Error('exclusions.yaml is read-only'));
    render(<ExcludeHostAction api={client} host="cdn.example.com" surface="flow table" />);
    await openConfirm();
    await userEvent.click(screen.getByRole('button', { name: 'Exclude this host' }));
    expect((await screen.findByRole('alert')).textContent).toContain(
      'exclusions.yaml is read-only',
    );
  });

  it('renders nothing for a flow with no host to exclude', () => {
    const { container } = render(
      <ExcludeHostAction api={api()} host={null} surface="flow table" />,
    );
    expect(container.innerHTML).toBe('');
  });

  it('dismisses its own message', async () => {
    render(<ExcludeHostAction api={api()} host="cdn.example.com" surface="flow table" />);
    await openConfirm();
    await userEvent.click(screen.getByRole('button', { name: 'Exclude this host' }));
    await screen.findByRole('status');
    await userEvent.click(
      screen.getByRole('button', { name: 'Dismiss the exclusion message for cdn.example.com' }),
    );
    expect(screen.queryByRole('status')).toBeNull();
  });

  it('does not open the flow behind it when clicked', async () => {
    // The action lives inside a table row whose click opens the detail panel.
    const rowClick = vi.fn();
    render(
      <div onClick={rowClick}>
        <ExcludeHostAction api={api()} host="cdn.example.com" surface="flow table" />
      </div>,
    );
    await openConfirm();
    await userEvent.click(screen.getByRole('button', { name: 'Exclude this host' }));
    await screen.findByRole('status');
    expect(rowClick).not.toHaveBeenCalled();
  });
});

describe('what it says afterwards depends on where you are', () => {
  const ENTRIES = { entries: [{ pattern: '*.apple.com', comment: 'pinning', source: 'default' }] };

  function client(): ApiClient {
    const api = new ApiClient('http://127.0.0.1:8081');
    vi.spyOn(api, 'getExclusions').mockResolvedValue(ENTRIES as never);
    vi.spyOn(api, 'putExclusions').mockResolvedValue(undefined as never);
    return api;
  }

  async function excludeFrom(live: boolean): Promise<string> {
    render(<ExcludeHostAction api={client()} host="cdn.example.com" surface="test" live={live} />);
    await userEvent.click(screen.getByRole('button', { name: /exclude/i }));
    await userEvent.click(screen.getByRole('button', { name: 'Exclude this host' }));
    return (await screen.findByRole('status')).textContent ?? '';
  }

  it('tells a live viewer to reload', async () => {
    expect(await excludeFrom(true)).toMatch(/Reload the page/);
  });

  it('does not tell a session viewer to reload', async () => {
    // Telling someone browsing a recorded session to reload is telling them to
    // expect a change that cannot happen there. The exclusion is the same;
    // only the honest thing to say afterwards differs.
    const message = await excludeFrom(false);
    expect(message).not.toMatch(/Reload the page/);
    expect(message).toMatch(/recorded session/);
  });

  it('says the same thing about the exclusion itself when live', async () => {
    expect(await excludeFrom(true)).toMatch(/tunnelled undecrypted/);
  });

  it('says the same thing about the exclusion itself on a session', async () => {
    // The exclusion is identical; only the advice afterwards differs.
    expect(await excludeFrom(false)).toMatch(/tunnelled undecrypted/);
  });
});
