/** The popup. SPEC-3 §5.1, REQ EXT-011. */
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { Popup } from './Popup';
import { DEFAULT_STATE, type DurableState } from '../shared/state';
import type { StatusReply } from '../shared/messages';

function status(
  overrides: Partial<StatusReply> = {},
  state: Partial<DurableState> = {},
): StatusReply {
  return {
    state: { ...DEFAULT_STATE, ...state },
    attributionGranted: false,
    daemonReachable: true,
    proxyControllable: true,
    controlLevel: 'controllable_by_this_extension',
    profiles: ['default'],
    counters: { flows: 42, blocked: 3, modified: 1, passthrough: 2 },
    version: '0.1.0',
    extensionVersion: '0.1.0',
    ...overrides,
  };
}

let sendMessage: ReturnType<typeof vi.fn>;

beforeEach(() => {
  sendMessage = vi.fn().mockResolvedValue(status());
  vi.stubGlobal('chrome', {
    runtime: { sendMessage },
    tabs: {
      query: vi.fn().mockResolvedValue([{ url: 'https://cdn.example.com/x' }]),
      create: vi.fn(),
    },
  });
});

afterEach(() => vi.unstubAllGlobals());

describe('Popup', () => {
  it('shows the daemon as up', async () => {
    render(<Popup />);
    await waitFor(() => expect(screen.getByText('daemon up')).toBeTruthy());
  });

  it('shows the daemon as down', async () => {
    sendMessage.mockResolvedValue(status({ daemonReachable: false }));
    render(<Popup />);
    await waitFor(() => expect(screen.getByText('daemon down')).toBeTruthy());
  });

  it('reflects the proxy toggle state', async () => {
    sendMessage.mockResolvedValue(status({}, { proxyEnabled: true, paired: true }));
    render(<Popup />);
    await waitFor(() =>
      expect(screen.getByRole('switch').getAttribute('aria-checked')).toBe('true'),
    );
  });

  it('turns the proxy on', async () => {
    sendMessage.mockResolvedValue(status({}, { paired: true }));
    render(<Popup />);
    await waitFor(() => expect(screen.getByRole('switch')).toBeTruthy());
    await userEvent.click(screen.getByRole('switch'));
    expect(sendMessage).toHaveBeenCalledWith({ type: 'set_proxy', enabled: true });
  });

  describe('a toggle the user cannot act on says why', () => {
    it('when the daemon is down', async () => {
      sendMessage.mockResolvedValue(status({ daemonReachable: false }, { paired: true }));
      render(<Popup />);
      await waitFor(() => expect(screen.getByText(/pporlock run/)).toBeTruthy());
      expect(screen.getByRole('switch').hasAttribute('disabled')).toBe(true);
    });

    it('when not yet paired', async () => {
      render(<Popup />);
      await waitFor(() => expect(screen.getByText(/Pair with the daemon first/)).toBeTruthy());
    });

    it('when another extension holds the proxy', async () => {
      sendMessage.mockResolvedValue(
        status(
          { proxyControllable: false, controlLevel: 'controlled_by_other_extensions' },
          { paired: true },
        ),
      );
      render(<Popup />);
      await waitFor(() => expect(screen.getByText(/Another extension/)).toBeTruthy());
    });

    it('when an enterprise policy holds it', async () => {
      sendMessage.mockResolvedValue(
        status(
          { proxyControllable: false, controlLevel: 'controlled_by_policy' },
          { paired: true },
        ),
      );
      render(<Popup />);
      await waitFor(() => expect(screen.getByText(/enterprise policy/)).toBeTruthy());
    });
  });

  it('explains a tripped fail-safe rather than just showing off', async () => {
    // REQ EXT-010 — the user must learn that pporlock turned itself off, not
    // discover it by finding the internet broken.
    sendMessage.mockResolvedValue(status({}, { paired: true, failSafeTrippedAt: Date.now() }));
    render(<Popup />);
    await waitFor(() => expect(screen.getByText(/pporlock turned the proxy off/)).toBeTruthy());
    expect(screen.getByText(/your browsing is working/i)).toBeTruthy();
  });

  it('dismisses the fail-safe notice', async () => {
    sendMessage.mockResolvedValue(status({}, { paired: true, failSafeTrippedAt: Date.now() }));
    render(<Popup />);
    await waitFor(() => expect(screen.getByText('dismiss')).toBeTruthy());
    await userEvent.click(screen.getByText('dismiss'));
    expect(sendMessage).toHaveBeenCalledWith({ type: 'dismiss_error' });
  });

  it('makes an active development toggle prominent', async () => {
    // REQ PXY-044 — these make production behaviour unreproducible.
    sendMessage.mockResolvedValue(
      status({}, { paired: true, devToggles: { anticache: true, anticomp: false } }),
    );
    render(<Popup />);
    await waitFor(() => expect(screen.getByText(/anticache active/)).toBeTruthy());
    expect(screen.getByText(/unreproducible/)).toBeTruthy();
  });

  it('offers to turn a development toggle off', async () => {
    sendMessage.mockResolvedValue(
      status({}, { paired: true, devToggles: { anticache: true, anticomp: false } }),
    );
    render(<Popup />);
    await waitFor(() => expect(screen.getByText('turn off anticache')).toBeTruthy());
    await userEvent.click(screen.getByText('turn off anticache'));
    expect(sendMessage).toHaveBeenCalledWith({
      type: 'set_dev_toggle',
      toggle: 'anticache',
      value: false,
    });
  });

  it('offers pairing when unpaired and the daemon is up', async () => {
    render(<Popup />);
    await waitFor(() => expect(screen.getByLabelText('Pairing code')).toBeTruthy());
    expect(screen.getByText(/pporlock pair/)).toBeTruthy();
  });

  it('submits a pairing code', async () => {
    render(<Popup />);
    await waitFor(() => expect(screen.getByLabelText('Pairing code')).toBeTruthy());
    await userEvent.type(screen.getByLabelText('Pairing code'), '1234');
    await userEvent.click(screen.getByRole('button', { name: 'pair' }));
    expect(sendMessage).toHaveBeenCalledWith({ type: 'pair', code: '1234' });
  });

  it('shows counters and says they are browser-wide without the grant', async () => {
    sendMessage.mockResolvedValue(status({ attributionGranted: false }, { paired: true }));
    render(<Popup />);
    await waitFor(() => expect(screen.getByText('42')).toBeTruthy());
    // A limitation that is stated is not a shortfall.
    expect(screen.getByText(/counts are browser-wide/)).toBeTruthy();
    expect(screen.getByText('enable per-tab counts')).toBeTruthy();
  });

  it('says per-tab attribution is on once the grant is held', async () => {
    sendMessage.mockResolvedValue(status({ attributionGranted: true }, { paired: true }));
    render(<Popup />);
    await waitFor(() => expect(screen.getByText(/per-tab attribution is on/)).toBeTruthy());
    expect(screen.queryByText('enable per-tab counts')).toBeNull();
  });

  it('requests the optional permission from the click itself', async () => {
    // chrome.permissions.request needs a user gesture, so it cannot be routed
    // through the service worker.
    const request = vi.fn().mockResolvedValue(true);
    const chromeStub = globalThis as unknown as { chrome: Record<string, unknown> };
    chromeStub.chrome['permissions'] = { request };
    sendMessage.mockResolvedValue(status({ attributionGranted: false }, { paired: true }));
    render(<Popup />);
    await waitFor(() => expect(screen.getByText('enable per-tab counts')).toBeTruthy());
    await userEvent.click(screen.getByText('enable per-tab counts'));
    expect(request).toHaveBeenCalledWith({ origins: ['<all_urls>'] });
  });

  it('offers to bypass the current host', async () => {
    sendMessage.mockResolvedValue(status({}, { paired: true }));
    render(<Popup />);
    await waitFor(() => expect(screen.getByText('cdn.example.com')).toBeTruthy());
    await userEvent.click(screen.getByRole('button', { name: 'bypass host' }));
    expect(sendMessage).toHaveBeenCalledWith({ type: 'bypass_host', host: 'cdn.example.com' });
  });

  it('does not offer bypass for a non-http tab', async () => {
    const chromeStub = globalThis as unknown as {
      chrome: { tabs: { query: unknown; create: unknown } };
    };
    chromeStub.chrome.tabs.query = vi.fn().mockResolvedValue([{ url: 'chrome://extensions' }]);
    sendMessage.mockResolvedValue(status({}, { paired: true }));
    render(<Popup />);
    await waitFor(() => expect(screen.getByText('daemon up')).toBeTruthy());
    expect(screen.queryByRole('button', { name: 'bypass host' })).toBeNull();
  });

  it('switches profile', async () => {
    sendMessage.mockResolvedValue(
      status({ profiles: ['default', 'ad-blocking'] }, { paired: true, activeProfile: 'default' }),
    );
    render(<Popup />);
    await waitFor(() => expect(screen.getByLabelText('Active profile')).toBeTruthy());
    await userEvent.selectOptions(screen.getByLabelText('Active profile'), 'ad-blocking');
    expect(sendMessage).toHaveBeenCalledWith({ type: 'activate_profile', name: 'ad-blocking' });
  });

  it('surfaces an action failure', async () => {
    sendMessage
      .mockResolvedValueOnce(status({}, { paired: true }))
      .mockResolvedValueOnce({ ok: false, error: 'Could not set the proxy: refused' })
      .mockResolvedValue(status({}, { paired: true }));
    render(<Popup />);
    await waitFor(() => expect(screen.getByRole('switch')).toBeTruthy());
    await userEvent.click(screen.getByRole('switch'));
    await waitFor(() => expect(screen.getByText(/Could not set the proxy/)).toBeTruthy());
  });

  it('opens the web UI', async () => {
    sendMessage.mockResolvedValue(status({}, { paired: true }));
    render(<Popup />);
    await waitFor(() => expect(screen.getByText('open web UI')).toBeTruthy());
    await userEvent.click(screen.getByText('open web UI'));
    const chromeStub = globalThis as unknown as {
      chrome: { tabs: { create: ReturnType<typeof vi.fn> } };
    };
    expect(chromeStub.chrome.tabs.create).toHaveBeenCalled();
  });
});

describe('recording from the popup (REQ CAP-025, EXT-023)', () => {
  it('offers a record control when paired and idle', async () => {
    sendMessage.mockResolvedValue(status({}, { paired: true }));
    render(<Popup />);
    expect(await screen.findByRole('button', { name: 'record' })).toBeTruthy();
    expect(screen.getByLabelText('Session name')).toBeTruthy();
  });

  it('starts a session with the name given', async () => {
    sendMessage.mockResolvedValue(status({}, { paired: true }));
    render(<Popup />);
    await userEvent.type(await screen.findByLabelText('Session name'), 'checkout-bug');
    await userEvent.click(screen.getByRole('button', { name: 'record' }));
    expect(sendMessage).toHaveBeenCalledWith({
      type: 'start_recording',
      name: 'checkout-bug',
    });
  });

  it('says unmissably that it is recording, and what that means', async () => {
    // Recording is opt-in and off by default (REQ CAP-020). A proxy quietly
    // writing every flow it sees to disk is a different, worse tool, so while
    // it is on the popup states it and says the file holds no real secrets.
    sendMessage.mockResolvedValue(status({}, { paired: true, recordingSession: 's-1' }));
    render(<Popup />);
    expect(await screen.findByText('● recording')).toBeTruthy();
    expect(screen.getByText(/masked as they are written/)).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'record' })).toBeNull();
  });

  it('stops the session', async () => {
    sendMessage.mockResolvedValue(status({}, { paired: true, recordingSession: 's-1' }));
    render(<Popup />);
    await userEvent.click(await screen.findByRole('button', { name: 'stop recording' }));
    expect(sendMessage).toHaveBeenCalledWith({ type: 'stop_recording' });
  });

  it('offers no recording control before pairing', async () => {
    // Nothing to record to. An enabled control that cannot work is worse than
    // its absence.
    sendMessage.mockResolvedValue(status({}, { paired: false }));
    render(<Popup />);
    await screen.findByText(/pporlock/);
    expect(screen.queryByRole('button', { name: 'record' })).toBeNull();
  });
});

/**
 * Which version is that? — OI-24.
 *
 * The footer used to render a bare `v0.1.0`, and it was the *daemon's*, read
 * from GET /state. The extension's own version never appeared anywhere.
 *
 * The two are built and installed separately: the extension is loaded unpacked
 * and goes stale the moment it is rebuilt without being reloaded, which is the
 * ordinary case while developing. An unlabelled number then reports the half
 * that was updated and says nothing about the half that was not — the reader
 * concludes they are running new code when they are not.
 */
describe('version display', () => {
  it('names both versions, so neither can be mistaken for the other', async () => {
    render(<Popup />);

    const footer = await screen.findByTitle('extension version / daemon version');

    expect(footer.textContent).toContain('ext 0.1.0');
    expect(footer.textContent).toContain('daemon 0.1.0');
  });

  it('flags a mismatch, because that is the case worth noticing', async () => {
    // A rebuilt-but-not-reloaded extension against a fresh daemon. Nothing else
    // in the UI would reveal it, and it is exactly the state that makes a fix
    // look like it did not work.
    sendMessage.mockResolvedValue(status({ version: '0.2.0', extensionVersion: '0.1.0' }));
    render(<Popup />);

    expect(
      await screen.findByTitle('the extension and daemon are different versions'),
    ).toBeTruthy();
  });

  it('does not flag a match', async () => {
    render(<Popup />);
    await screen.findByTitle('extension version / daemon version');

    expect(screen.queryByTitle('the extension and daemon are different versions')).toBeNull();
  });

  it('says the daemon is unknown rather than showing nothing', async () => {
    // Absent is not zero. A blank slot reads as "no version", which is wrong —
    // the extension has one and is telling you the daemon did not answer.
    sendMessage.mockResolvedValue(status({ version: null, daemonReachable: false }));
    render(<Popup />);

    const footer = await screen.findByTitle('extension version / daemon version');

    expect(footer.textContent).toContain('ext 0.1.0');
    expect(footer.textContent).toContain('daemon —');
  });
});
