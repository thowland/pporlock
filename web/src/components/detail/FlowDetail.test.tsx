/** Flow detail. SPEC-2 §6, REQ WUI-004. */
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiClient } from '../../api/client';
import { FlowDetail } from './FlowDetail';
import { makeFlow, makeProvenance } from '../../test/factories';
import type { FlowRecord } from '../../api/types';

function api(detailed?: FlowRecord): ApiClient {
  const client = new ApiClient('http://127.0.0.1:8081');
  vi.spyOn(client, 'getFlow').mockResolvedValue(detailed ?? makeFlow());
  return client;
}

afterEach(() => vi.restoreAllMocks());

describe('FlowDetail', () => {
  it('opens on the provenance tab', async () => {
    // It is the reason the panel exists, so it is not one click away.
    render(<FlowDetail flow={makeFlow()} api={api()} onClose={() => {}} />);
    await waitFor(() =>
      expect(screen.getByRole('tab', { name: /provenance/ }).getAttribute('aria-selected')).toBe(
        'true',
      ),
    );
  });

  it('shows the URL and status', async () => {
    render(<FlowDetail flow={makeFlow()} api={api()} onClose={() => {}} />);
    expect(screen.getByText('200')).toBeTruthy();
    await waitFor(() =>
      expect(screen.getByTitle('https://cdn.example.com/a/analytics.js')).toBeTruthy(),
    );
  });

  it('fetches full detail with bodies when opened', async () => {
    // The list carries summary detail; bodies cost too much to send per row.
    const client = api();
    render(<FlowDetail flow={makeFlow()} api={client} onClose={() => {}} />);
    await waitFor(() => expect(client.getFlow).toHaveBeenCalledWith('f0', 'bodies'));
  });

  it('still renders when the detail fetch fails', async () => {
    const client = new ApiClient('http://127.0.0.1:8081');
    vi.spyOn(client, 'getFlow').mockRejectedValue(new Error('gone'));
    render(<FlowDetail flow={makeFlow()} api={client} onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText('200')).toBeTruthy());
  });

  it('closes', async () => {
    const onClose = vi.fn();
    render(<FlowDetail flow={makeFlow()} api={api()} onClose={onClose} />);
    await userEvent.click(screen.getByLabelText('Close detail'));
    expect(onClose).toHaveBeenCalled();
  });

  it('badges the provenance tab with the note count', async () => {
    const flow = makeFlow({
      provenance: makeProvenance({
        notes: [{ code: 'csp_modified', severity: 'warning', message: 'x' }],
      }),
    });
    render(<FlowDetail flow={flow} api={api(flow)} onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText('1')).toBeTruthy());
  });

  describe('tabs', () => {
    it('overview shows identity and flags', async () => {
      render(<FlowDetail flow={makeFlow()} api={api()} onClose={() => {}} />);
      await userEvent.click(screen.getByRole('tab', { name: 'overview' }));
      expect(screen.getByText('flow')).toBeTruthy();
      expect(screen.getByText('f0')).toBeTruthy();
    });

    it('overview reports an unattributed flow as such', async () => {
      render(<FlowDetail flow={makeFlow({ tab_id: null })} api={api()} onClose={() => {}} />);
      await userEvent.click(screen.getByRole('tab', { name: 'overview' }));
      expect(screen.getByText('unattributed')).toBeTruthy();
    });

    it('overview explains a tunnelled connection', async () => {
      const flow = makeFlow({
        kind: 'passthrough',
        request: undefined,
        response: undefined,
        passthrough: {
          host: 'www.chase.com',
          ip: null,
          pattern: '*.chase.com',
          reason: 'sensitive: financial',
        },
      });
      render(<FlowDetail flow={flow} api={api(flow)} onClose={() => {}} />);
      await userEvent.click(screen.getByRole('tab', { name: 'overview' }));
      await waitFor(() => expect(screen.getByText('*.chase.com')).toBeTruthy());
      expect(screen.getByText('sensitive: financial')).toBeTruthy();
    });

    it('request shows headers', async () => {
      const flow = makeFlow();
      flow.request!.headers = [['accept', '*/*']];
      render(<FlowDetail flow={flow} api={api(flow)} onClose={() => {}} />);
      await userEvent.click(screen.getByRole('tab', { name: 'request' }));
      await waitFor(() => expect(screen.getByText('accept')).toBeTruthy());
    });

    it('response says a streamed body was never buffered', async () => {
      // Different from "no body", and the reason a transform may not have run.
      const flow = makeFlow();
      flow.response!.streamed = true;
      render(<FlowDetail flow={flow} api={api(flow)} onClose={() => {}} />);
      await userEvent.click(screen.getByRole('tab', { name: 'response' }));
      await waitFor(() =>
        expect(screen.getByText(/streamed, so its body was never buffered/)).toBeTruthy(),
      );
    });

    it('response warns when a body was truncated', async () => {
      const flow = makeFlow();
      flow.response!.body = 'partial';
      flow.response!.body_truncated = true;
      render(<FlowDetail flow={flow} api={api(flow)} onClose={() => {}} />);
      await userEvent.click(screen.getByRole('tab', { name: 'response' }));
      await waitFor(() => expect(screen.getByText(/Truncated at the capture cap/)).toBeTruthy());
    });

    it('response reports a body withheld at this detail level', async () => {
      const flow = makeFlow();
      flow.response!.body = null;
      render(<FlowDetail flow={flow} api={api(flow)} onClose={() => {}} />);
      await userEvent.click(screen.getByRole('tab', { name: 'response' }));
      await waitFor(() =>
        expect(screen.getByText(/Body not included at this detail level/)).toBeTruthy(),
      );
    });
  });

  describe('redaction', () => {
    it('renders a masked value distinctly rather than as literal text', async () => {
      // SPEC-0 §9.1 — the fingerprint answers "is this the same token" without
      // revealing either.
      const flow = makeFlow();
      flow.request!.headers = [['cookie', '«redacted:sha1=a3f2,len=142»']];
      render(<FlowDetail flow={flow} api={api(flow)} onClose={() => {}} />);
      await userEvent.click(screen.getByRole('tab', { name: 'request' }));
      await waitFor(() => expect(screen.getByText('redacted')).toBeTruthy());
      // Same length-and-fingerprint presentation the extension uses, so the
      // two clients agree about what a masked value looks like.
      expect(screen.getByText(/142 bytes · #a3f2/)).toBeTruthy();
    });

    it('leaves an ordinary header alone', async () => {
      const flow = makeFlow();
      flow.request!.headers = [['accept', 'text/html']];
      render(<FlowDetail flow={flow} api={api(flow)} onClose={() => {}} />);
      await userEvent.click(screen.getByRole('tab', { name: 'request' }));
      await waitFor(() => expect(screen.getByText('text/html')).toBeTruthy());
    });
  });
});
