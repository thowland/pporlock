/** Browsing a recorded session. SPEC-2 §8.2, REQ CAP-021, CAP-045, WUI-010. */
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { SessionBrowser } from './SessionBrowser';
import { FlowDetail } from '../detail/FlowDetail';
import { ApiClient } from '../../api/client';
import { makeFlow, makeProvenance, makeSession } from '../../test/factories';
import type { FlowRecord, SessionMeta } from '../../api/types';

afterEach(() => vi.restoreAllMocks());

function maskedFlow(): FlowRecord {
  const flow = makeFlow({ flow_id: 'rec-1', redacted: true });
  flow.request!.headers = [['cookie', '«redacted:sha1=a3f2,len=142»']];
  flow.response!.headers = [['set-cookie', '«redacted:sha1=bb01,len=64»']];
  flow.provenance = makeProvenance({ profile: 'default' });
  return flow;
}

function api(flows: FlowRecord[] = [maskedFlow()], meta: SessionMeta = makeSession()): ApiClient {
  const client = new ApiClient('http://127.0.0.1:8081');
  vi.spyOn(client, 'getSession').mockResolvedValue(meta);
  vi.spyOn(client, 'listSessionFlows').mockResolvedValue({
    flows,
    next_cursor: null,
    total_estimate: flows.length,
  });
  // Spied so the assertions below can prove they are never called.
  vi.spyOn(client, 'unmask').mockResolvedValue({
    flow_id: 'rec-1',
    field_path: 'request.headers.cookie',
    value: 'sid=the-real-secret',
  });
  vi.spyOn(client, 'getFlow').mockRejectedValue(new Error('not in the live ring'));
  return client;
}

function browser(client: ApiClient) {
  return <SessionBrowser api={client} sessionId="s1" onBack={() => {}} onDryRun={() => {}} />;
}

describe('SessionBrowser  # REQ CAP-021, WUI-010', () => {
  it('reads flows from the session, not the live ring', async () => {
    const client = api();
    render(browser(client));
    await screen.findByText('cdn.example.com');
    expect(client.listSessionFlows).toHaveBeenCalledWith('s1', {}, { limit: 500, detail: 'full' });
  });

  it('shows the session identity and warns when flows were dropped', async () => {
    render(browser(api([maskedFlow()], makeSession({ dropped: 12 }))));
    await screen.findByText('checkout-bug');
    // An incomplete recording changes what a dry run against it means
    // (REQ CAP-023), so it is stated rather than left in the metadata.
    expect(screen.getByRole('status').textContent).toContain('12 flows were dropped');
  });

  it('reuses the live flow table and provenance view', async () => {
    // SPEC-2 §8.2: one implementation, differing only in data source. This
    // test passes because SessionBrowser imports FlowTable/FlowDetail — if a
    // second copy is ever introduced, the components diverge silently, so the
    // assertion is that the shared detail panel renders here at all.
    render(browser(api()));
    await userEvent.click(await screen.findByText('cdn.example.com'));
    await screen.findByRole('tab', { name: /provenance/ });
    expect(screen.getByLabelText('Flow detail')).toBeTruthy();
  });

  it('offers no unmask control on a session flow  # REQ CAP-043, CAP-045', async () => {
    // The load-bearing test. Unmasking is live-ring-only, web-UI-only, one
    // value at a time (SPEC-0 §9.3) — and a session flow never held the real
    // value, because redaction happens at write time (REQ CAP-045). A reveal
    // control here would either be a lie or a leak.
    const client = api();
    render(browser(client));
    await userEvent.click(await screen.findByText('cdn.example.com'));

    for (const tab of ['request', 'response']) {
      await userEvent.click(screen.getByRole('tab', { name: tab }));
      await waitFor(() => expect(screen.getAllByText(/redacted/).length).toBeGreaterThan(0));
      // Masked values are shown, with their length and fingerprint...
      expect(screen.getByText(/142 bytes · #a3f2|64 bytes · #bb01/)).toBeTruthy();
      // ...and nothing anywhere offers to reveal them.
      expect(screen.queryByRole('button', { name: /reveal/i })).toBeNull();
      expect(screen.queryByText(/^reveal$/i)).toBeNull();
    }
    expect(client.unmask).not.toHaveBeenCalled();
  });

  it('says plainly why values here cannot be revealed', async () => {
    render(browser(api()));
    expect(
      (await screen.findByTitle(/redacted before it was written to disk/)).textContent,
    ).toContain('cannot be revealed');
  });

  it('does not fetch session flow detail from the live flow route', async () => {
    // `GET /flows/{id}` would 404 for a recorded flow; the record from the
    // session page is the one we have.
    const client = api();
    render(browser(client));
    await userEvent.click(await screen.findByText('cdn.example.com'));
    await screen.findByLabelText('Flow detail');
    expect(client.getFlow).not.toHaveBeenCalled();
  });

  it('surfaces a read failure instead of showing an empty session', async () => {
    const client = api();
    vi.spyOn(client, 'listSessionFlows').mockRejectedValue(new Error('session file is corrupt'));
    render(browser(client));
    expect((await screen.findByRole('alert')).textContent).toContain('session file is corrupt');
  });

  it('pushes filters at the session query rather than hiding rows', async () => {
    const client = api();
    render(browser(client));
    await screen.findByText('cdn.example.com');
    await userEvent.type(screen.getByLabelText('Filter by host'), 'a.example');
    await waitFor(() =>
      expect(client.listSessionFlows).toHaveBeenLastCalledWith(
        's1',
        { host: 'a.example' },
        { limit: 500, detail: 'full' },
      ),
    );
  });

  it('omits the live-only pause and clear controls', async () => {
    // Pausing a file and clearing a file are both meaningless.
    render(browser(api()));
    await screen.findByText('cdn.example.com');
    expect(screen.queryByRole('button', { name: 'pause' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'clear' })).toBeNull();
  });
});

describe('FlowDetail unmask gate  # REQ CAP-043', () => {
  it('renders a reveal control only when a live unmask callback is supplied', async () => {
    // The gate is the presence of the callback, not a flag that a later edit
    // could invert: a session view cannot supply one, so it cannot render one.
    const flow = maskedFlow();
    const client = api([flow]);
    vi.spyOn(client, 'getFlow').mockResolvedValue(flow);

    const { unmount } = render(<FlowDetail flow={flow} api={client} onClose={() => {}} />);
    await userEvent.click(screen.getByRole('tab', { name: 'request' }));
    expect(screen.queryByRole('button', { name: /reveal/i })).toBeNull();
    unmount();

    render(
      <FlowDetail
        flow={flow}
        api={client}
        onClose={() => {}}
        onUnmask={(path) => client.unmask(flow.flow_id, path).then((r) => r.value)}
      />,
    );
    await userEvent.click(screen.getByRole('tab', { name: 'request' }));
    const reveal = await screen.findByRole('button', {
      name: 'Reveal request.headers.cookie',
    });
    await userEvent.click(reveal);
    expect(await screen.findByText(/sid=the-real-secret/)).toBeTruthy();
    // One value at a time: the request cookie was named explicitly.
    expect(client.unmask).toHaveBeenCalledWith('rec-1', 'request.headers.cookie');
    expect(client.unmask).toHaveBeenCalledTimes(1);
  });

  it('addresses a repeated header by its occurrence among headers of that name', async () => {
    const flow = maskedFlow();
    flow.response!.headers = [
      ['set-cookie', '«redacted:sha1=bb01,len=64»'],
      ['content-type', 'text/html'],
      ['set-cookie', '«redacted:sha1=cc02,len=70»'],
    ];
    const client = api([flow]);
    vi.spyOn(client, 'getFlow').mockResolvedValue(flow);
    render(
      <FlowDetail
        flow={flow}
        api={client}
        onClose={() => {}}
        onUnmask={(path) => client.unmask(flow.flow_id, path).then((r) => r.value)}
      />,
    );
    await userEvent.click(screen.getByRole('tab', { name: 'response' }));
    // The second Set-Cookie is `.1`, not `.2` — it is indexed among Set-Cookie
    // headers, not among rows (daemon redact.py:_from_headers).
    expect(screen.getByRole('button', { name: 'Reveal response.headers.set-cookie' })).toBeTruthy();
    expect(
      screen.getByRole('button', { name: 'Reveal response.headers.set-cookie.1' }),
    ).toBeTruthy();
  });

  it('reports a refused reveal rather than failing silently', async () => {
    const flow = maskedFlow();
    const client = api([flow]);
    vi.spyOn(client, 'getFlow').mockResolvedValue(flow);
    render(
      <FlowDetail
        flow={flow}
        api={client}
        onClose={() => {}}
        onUnmask={() => Promise.reject(new Error('flow has aged out of the ring'))}
      />,
    );
    await userEvent.click(screen.getByRole('tab', { name: 'request' }));
    await userEvent.click(screen.getByRole('button', { name: /reveal/i }));
    expect((await screen.findByRole('alert')).textContent).toContain('aged out of the ring');
  });

  it('hides a revealed value again on request', async () => {
    const flow = maskedFlow();
    const client = api([flow]);
    vi.spyOn(client, 'getFlow').mockResolvedValue(flow);
    render(
      <FlowDetail
        flow={flow}
        api={client}
        onClose={() => {}}
        onUnmask={() => Promise.resolve('sid=the-real-secret')}
      />,
    );
    await userEvent.click(screen.getByRole('tab', { name: 'request' }));
    await userEvent.click(screen.getByRole('button', { name: /reveal/i }));
    await userEvent.click(await screen.findByRole('button', { name: 'hide' }));
    expect(screen.queryByText(/sid=the-real-secret/)).toBeNull();
    expect(screen.getByText(/142 bytes · #a3f2/)).toBeTruthy();
  });
});
