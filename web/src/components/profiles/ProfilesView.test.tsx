import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ProfilesView } from './ProfilesView';
import { ApiClient } from '../../api/client';
import type { ProfileSummary } from '../../api/types';

afterEach(() => vi.restoreAllMocks());

const PROFILES: ProfileSummary[] = [
  { name: 'default', modules: [] },
  {
    name: 'ad-blocking',
    description: 'Everyday browsing',
    modules: ['block-vendors', 'strip-sri'],
    dev_toggles: { anticache: false, anticomp: true },
  },
];

function api(profiles = PROFILES, active = 'default'): ApiClient {
  const client = new ApiClient('http://127.0.0.1:8081');
  vi.spyOn(client, 'listProfiles').mockResolvedValue({ profiles, active });
  vi.spyOn(client, 'createProfile').mockResolvedValue({ name: 'new' });
  vi.spyOn(client, 'deleteProfile').mockResolvedValue(undefined);
  vi.spyOn(client, 'activateProfile').mockResolvedValue({ active: 'ad-blocking' });
  return client;
}

describe('ProfilesView  # REQ WUI-009', () => {
  it('lists profiles with module counts and marks the active one', async () => {
    render(<ProfilesView api={api()} />);
    await waitFor(() => expect(screen.getByText('ad-blocking')).toBeTruthy());
    const row = screen.getByText('default').closest('tr')!;
    expect(within(row).getByText('active')).toBeTruthy();
    expect(screen.getByText('Everyday browsing')).toBeTruthy();
  });

  it('flags a profile that carries a dev toggle before it is activated', async () => {
    render(<ProfilesView api={api()} />);
    await waitFor(() => expect(screen.getByText('⚠ anticomp')).toBeTruthy());
  });

  it('creates a profile', async () => {
    const client = api();
    render(<ProfilesView api={client} />);
    await screen.findByText('ad-blocking');
    await userEvent.type(screen.getByLabelText('New profile name'), '  staging  ');
    await userEvent.click(screen.getByRole('button', { name: 'Create profile' }));
    expect(client.createProfile).toHaveBeenCalledWith({ name: 'staging', modules: [] });
  });

  it('refuses to create a nameless profile', async () => {
    const client = api();
    render(<ProfilesView api={client} />);
    await screen.findByText('ad-blocking');
    await userEvent.click(screen.getByRole('button', { name: 'Create profile' }));
    expect(screen.getByRole('alert').textContent).toContain('needs a name');
    expect(client.createProfile).not.toHaveBeenCalled();
  });

  it('activates a profile  # REQ MOD-042', async () => {
    const client = api();
    const onActivated = vi.fn();
    render(<ProfilesView api={client} onActivated={onActivated} />);
    await userEvent.click(await screen.findByLabelText('Activate ad-blocking'));
    expect(client.activateProfile).toHaveBeenCalledWith('ad-blocking');
    await waitFor(() => expect(onActivated).toHaveBeenCalledWith('ad-blocking'));
  });

  it('cannot activate the profile already active', async () => {
    render(<ProfilesView api={api()} />);
    expect(await screen.findByLabelText('Activate default')).toHaveProperty('disabled', true);
  });

  it('deletes a profile', async () => {
    const client = api();
    render(<ProfilesView api={client} />);
    await userEvent.click(await screen.findByLabelText('Delete ad-blocking'));
    expect(client.deleteProfile).toHaveBeenCalledWith('ad-blocking');
  });

  // REQ MOD-041: default always exists, and the UI states the rule rather than
  // letting the user discover it through a failed request.
  it('will not delete the default profile', async () => {
    const client = api();
    render(<ProfilesView api={client} />);
    const control = await screen.findByLabelText('Delete default');
    expect(control).toHaveProperty('disabled', true);
    expect(control.getAttribute('title')).toBe('The default profile cannot be deleted');
    expect(screen.getByTitle('default cannot be renamed or deleted')).toBeTruthy();
    await userEvent.click(control);
    expect(client.deleteProfile).not.toHaveBeenCalled();
  });

  it('says so when profiles cannot be read', async () => {
    const client = new ApiClient('http://127.0.0.1:8081');
    vi.spyOn(client, 'listProfiles').mockRejectedValue(new Error('daemon unreachable'));
    render(<ProfilesView api={client} />);
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('unreachable'));
  });

  it('surfaces a failed activation', async () => {
    const client = api();
    vi.spyOn(client, 'activateProfile').mockRejectedValue(new Error('profile missing'));
    render(<ProfilesView api={client} />);
    await userEvent.click(await screen.findByLabelText('Activate ad-blocking'));
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('profile missing'));
  });

  it('seeds the active profile from daemon state before the list arrives', async () => {
    const client = api(PROFILES, 'ad-blocking');
    render(<ProfilesView api={client} activeProfile="ad-blocking" />);
    const row = (await screen.findByText('ad-blocking')).closest('tr')!;
    expect(within(row).getByText('active')).toBeTruthy();
  });
});
