/** Session list. SPEC-2 §8.2, REQ CAP-020, CAP-021, CAP-023, CAP-024. */
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { SessionsView } from './SessionsView';
import { ApiClient } from '../../api/client';
import { makeSession } from '../../test/factories';
import type { SessionMeta } from '../../api/types';

afterEach(() => vi.restoreAllMocks());

function api(sessions: SessionMeta[] = [makeSession()]): ApiClient {
  const client = new ApiClient('http://127.0.0.1:8081');
  vi.spyOn(client, 'listSessions').mockResolvedValue(sessions);
  vi.spyOn(client, 'startRecording').mockResolvedValue(makeSession({ state: 'recording' }));
  vi.spyOn(client, 'stopRecording').mockResolvedValue(makeSession());
  vi.spyOn(client, 'deleteSession').mockResolvedValue(undefined);
  return client;
}

function view(client: ApiClient) {
  return <SessionsView api={client} onOpen={() => {}} onDryRun={() => {}} />;
}

describe('SessionsView  # REQ CAP-021, WUI-010', () => {
  it('lists sessions with flow count, size and the active profile', async () => {
    render(view(api()));
    const row = (await screen.findByText('checkout-bug')).closest('tr')!;
    expect(within(row).getByText('42')).toBeTruthy();
    expect(within(row).getByText('200.0k')).toBeTruthy();
    expect(within(row).getByText('default')).toBeTruthy();
  });

  it('marks a session whose flows were dropped by writer overflow', async () => {
    // A session with drops is not a faithful recording (REQ CAP-023), and the
    // count says how far from faithful — colour alone would say neither.
    render(view(api([makeSession({ dropped: 7 })])));
    expect((await screen.findByText(/7 dropped/)).textContent).toContain('dropped');
  });

  it('starts a named recording, and refuses a nameless one  # REQ CAP-020', async () => {
    const client = api();
    render(view(client));
    await screen.findByText('checkout-bug');

    await userEvent.click(screen.getByRole('button', { name: 'Start recording' }));
    expect(screen.getByRole('alert').textContent).toContain('needs a name');
    expect(client.startRecording).not.toHaveBeenCalled();

    await userEvent.type(screen.getByLabelText('New recording name'), '  login-bug  ');
    await userEvent.click(screen.getByRole('button', { name: 'Start recording' }));
    expect(client.startRecording).toHaveBeenCalledWith('login-bug');
  });

  it('offers stop while recording and export once stopped', async () => {
    const client = api([makeSession({ state: 'recording', stopped_at: null })]);
    render(view(client));
    await screen.findByText('checkout-bug');
    expect(screen.getByText('● recording')).toBeTruthy();
    expect(screen.queryByRole('link', { name: 'Export HAR' })).toBeNull();

    await userEvent.click(screen.getByRole('button', { name: 'Stop' }));
    expect(client.stopRecording).toHaveBeenCalledWith('s1');
  });

  it('warns that a HAR export cannot carry provenance  # REQ CAP-024', async () => {
    render(view(api()));
    const har = await screen.findByRole('link', { name: 'Export HAR' });
    expect(har.getAttribute('title')).toContain('HAR cannot represent provenance');
    expect(har.getAttribute('href')).toContain('format=har');
    const native = screen.getByRole('link', { name: 'Export' });
    expect(native.getAttribute('href')).toContain('format=pporlock');
  });

  it('confirms a delete and says how much space it reclaims', async () => {
    const client = api();
    render(view(client));
    await userEvent.click(await screen.findByRole('button', { name: 'Delete checkout-bug' }));
    expect(screen.getByRole('alert').textContent).toContain('200.0k reclaimed');
    expect(client.deleteSession).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole('button', { name: 'Confirm delete' }));
    await waitFor(() => expect(client.deleteSession).toHaveBeenCalledWith('s1'));
  });

  it('lets a confirmation be cancelled', async () => {
    const client = api();
    render(view(client));
    await userEvent.click(await screen.findByRole('button', { name: 'Delete checkout-bug' }));
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('alert')).toBeNull();
    expect(client.deleteSession).not.toHaveBeenCalled();
  });

  it('routes to the browser and the dry run for a session', async () => {
    const opened: string[] = [];
    const dryRun: string[] = [];
    render(
      <SessionsView
        api={api()}
        onOpen={(id) => opened.push(id)}
        onDryRun={(id) => dryRun.push(id)}
      />,
    );
    await userEvent.click(await screen.findByRole('button', { name: 'Browse' }));
    await userEvent.click(screen.getByRole('button', { name: 'Dry run' }));
    expect(opened).toEqual(['s1']);
    expect(dryRun).toEqual(['s1']);
  });

  it('explains an empty list rather than showing a bare table', async () => {
    render(view(api([])));
    expect((await screen.findByText(/No recorded sessions/)).textContent).toBeTruthy();
    expect(screen.getByText(/Recording is off by default/)).toBeTruthy();
  });

  it('reports a listing failure', async () => {
    const client = api();
    vi.spyOn(client, 'listSessions').mockRejectedValue(new Error('state dir is unreadable'));
    render(view(client));
    expect((await screen.findByRole('alert')).textContent).toContain('state dir is unreadable');
  });

  it('reports a failure to start recording', async () => {
    const client = api();
    vi.spyOn(client, 'startRecording').mockRejectedValue(new Error('already recording'));
    render(view(client));
    await screen.findByText('checkout-bug');
    await userEvent.type(screen.getByLabelText('New recording name'), 'x');
    await userEvent.click(screen.getByRole('button', { name: 'Start recording' }));
    expect((await screen.findByRole('alert')).textContent).toContain('already recording');
  });
});
