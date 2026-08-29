/** Settings — the exclusion list. SPEC-2 §9, REQ PXY-014/016. */
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ExclusionsSection } from './ExclusionsSection';
import { ApiClient } from '../../api/client';
import type { ExclusionEntry } from '../../api/types';

afterEach(() => vi.restoreAllMocks());

const ENTRIES: ExclusionEntry[] = [
  { pattern: '*.apple.com', comment: 'update: macOS software update', source: 'default' },
  { pattern: 'cdn.example.com', comment: 'added from the flow table', source: 'user' },
  { pattern: 'mystery.test' },
];

function api(entries: ExclusionEntry[] = ENTRIES): ApiClient {
  const client = new ApiClient('http://127.0.0.1:8081');
  vi.spyOn(client, 'getExclusions').mockResolvedValue({ entries });
  vi.spyOn(client, 'putExclusions').mockImplementation((next) =>
    Promise.resolve({ entries: next }),
  );
  return client;
}

function row(pattern: string): HTMLElement {
  return screen.getByText(pattern).closest('li')!;
}

describe('ExclusionsSection  # REQ PXY-014', () => {
  it('lists the effective entries with their source and reason', async () => {
    render(<ExclusionsSection api={api()} />);
    await screen.findByText('*.apple.com');
    const shipped = row('*.apple.com');
    expect(within(shipped).getByText('default')).toBeTruthy();
    expect(within(shipped).getByText('update: macOS software update')).toBeTruthy();
    expect(within(row('cdn.example.com')).getByText('user')).toBeTruthy();
  });

  it('calls out an entry with no recorded reason', async () => {
    // An exclusion nobody can explain is indistinguishable from a bug.
    render(<ExclusionsSection api={api()} />);
    await screen.findByText('mystery.test');
    expect(within(row('mystery.test')).getByText('no reason recorded')).toBeTruthy();
  });

  it("removes the user's own entry in one click, preserving the rest", async () => {
    const client = api();
    render(<ExclusionsSection api={client} />);
    await screen.findByText('cdn.example.com');
    await userEvent.click(
      screen.getByRole('button', { name: 'Remove cdn.example.com from the exclusion list' }),
    );

    const sent = vi.mocked(client.putExclusions).mock.calls[0]?.[0] as ExclusionEntry[];
    expect(sent.map((e) => e.pattern)).toEqual(['*.apple.com', 'mystery.test']);
    expect((await screen.findByRole('status')).textContent).toContain('no longer excluded');
  });

  it('makes removing a shipped default take a second, informed click', async () => {
    // The defaults exist because interception breaks those hosts.
    const client = api();
    render(<ExclusionsSection api={client} />);
    await screen.findByText('*.apple.com');
    await userEvent.click(
      screen.getByRole('button', { name: 'Remove *.apple.com from the exclusion list' }),
    );
    expect(vi.mocked(client.putExclusions)).not.toHaveBeenCalled();
    expect(within(row('*.apple.com')).getByText(/pinned client fails closed/)).toBeTruthy();

    await userEvent.click(
      screen.getByRole('button', { name: 'Confirm removing the default exclusion *.apple.com' }),
    );
    const sent = vi.mocked(client.putExclusions).mock.calls[0]?.[0] as ExclusionEntry[];
    expect(sent.map((e) => e.pattern)).toEqual(['cdn.example.com', 'mystery.test']);
  });

  it('lets the user keep a default after all', async () => {
    const client = api();
    render(<ExclusionsSection api={client} />);
    await screen.findByText('*.apple.com');
    await userEvent.click(
      screen.getByRole('button', { name: 'Remove *.apple.com from the exclusion list' }),
    );
    await userEvent.click(screen.getByRole('button', { name: 'Keep *.apple.com' }));
    expect(vi.mocked(client.putExclusions)).not.toHaveBeenCalled();
    expect(screen.queryByText(/pinned client fails closed/)).toBeNull();
  });

  it('re-reads the list before writing it back', async () => {
    // Another tab, the extension, or the CLI may have added a host since this
    // screen loaded; PUT replaces everything, so a stale copy would drop it.
    const client = api();
    render(<ExclusionsSection api={client} />);
    await screen.findByText('cdn.example.com');
    vi.mocked(client.getExclusions).mockResolvedValue({
      entries: [...ENTRIES, { pattern: 'late.test', comment: 'added from the extension popup' }],
    });
    await userEvent.click(
      screen.getByRole('button', { name: 'Remove cdn.example.com from the exclusion list' }),
    );
    const sent = vi.mocked(client.putExclusions).mock.calls[0]?.[0] as ExclusionEntry[];
    expect(sent.map((e) => e.pattern)).toContain('late.test');
  });

  it('says so when the list cannot be read, rather than showing an empty one', async () => {
    const client = api();
    vi.spyOn(client, 'getExclusions').mockRejectedValue(new Error('daemon unreachable'));
    render(<ExclusionsSection api={client} />);
    expect((await screen.findByText(/could not be read/)).textContent).toContain(
      'daemon unreachable',
    );
    expect(screen.queryByRole('list')).toBeNull();
  });

  it('reports a write failure', async () => {
    const client = api();
    vi.spyOn(client, 'putExclusions').mockRejectedValue(new Error('exclusions.yaml is read-only'));
    render(<ExclusionsSection api={client} />);
    await screen.findByText('cdn.example.com');
    await userEvent.click(
      screen.getByRole('button', { name: 'Remove cdn.example.com from the exclusion list' }),
    );
    expect((await screen.findByRole('alert')).textContent).toContain('read-only');
  });

  it('states that an empty list means everything is decrypted', async () => {
    render(<ExclusionsSection api={api([])} />);
    expect(
      (await screen.findByText(/every connection is being decrypted/)).textContent,
    ).toBeTruthy();
  });
});
