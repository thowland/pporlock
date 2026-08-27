/**
 * EventStream behaviour against a mocked streaming fetch.
 *
 * The reason this is worth testing rather than trusting EventSource: we do not
 * use EventSource, because it cannot set an Authorization header and would force
 * the token into the URL. That trade means reconnection is ours to get right.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiClient } from './client';
import { EventStream } from './events';

const ORIGIN = 'http://127.0.0.1:8081';

function streamOf(chunks: string[], { hang = false } = {}): Response {
  const encoder = new TextEncoder();
  let index = 0;
  return {
    ok: true,
    status: 200,
    body: {
      getReader: () => ({
        read: async () => {
          if (index < chunks.length) {
            return { done: false, value: encoder.encode(chunks[index++]) };
          }
          if (hang) await new Promise(() => {});
          return { done: true, value: undefined };
        },
      }),
    },
  } as unknown as Response;
}

function client(): ApiClient {
  const api = new ApiClient(ORIGIN);
  api.setToken('secret');
  return api;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('EventStream', () => {
  it('delivers parsed events', async () => {
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValue(
          streamOf(
            [
              ': connected\n\n',
              'id: 1\nevent: flow.completed\ndata: {"type":"flow.completed","seq":1,"ts":"t","data":{"flow_id":"f0"}}\n\n',
            ],
            { hang: true },
          ),
        ),
    );

    const stream = new EventStream(client());
    const received: unknown[] = [];
    stream.on((e) => received.push(e));
    stream.connect();

    await vi.waitFor(() => expect(received).toHaveLength(1));
    stream.disconnect();
    expect((received[0] as { type: string }).type).toBe('flow.completed');
  });

  it('sends the token as a header and never in the URL', async () => {
    const fetchMock = vi.fn().mockResolvedValue(streamOf([], { hang: true }));
    vi.stubGlobal('fetch', fetchMock);

    const stream = new EventStream(client());
    stream.connect();
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).not.toContain('secret');
    expect((init.headers as Record<string, string>)['Authorization']).toBe('Bearer secret');
    stream.disconnect();
  });

  it('reports open once the stream is flowing', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(streamOf([': connected\n\n'], { hang: true })),
    );
    const stream = new EventStream(client());
    stream.connect();
    await vi.waitFor(() => expect(stream.state).toBe('open'));
    stream.disconnect();
  });

  it('reports closed after disconnect', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(streamOf([], { hang: true })));
    const stream = new EventStream(client());
    stream.connect();
    await vi.waitFor(() => expect(stream.state).toBe('open'));
    stream.disconnect();
    expect(stream.state).toBe('closed');
  });

  it('handles a frame split across chunks', async () => {
    // TCP does not respect frame boundaries; a naive parser drops events here.
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValue(
          streamOf(
            [
              'id: 1\nevent: flow.completed\ndata: {"type":"flow.comp',
              'leted","seq":1,"ts":"t","data":{}}\n\n',
            ],
            { hang: true },
          ),
        ),
    );

    const stream = new EventStream(client());
    const received: unknown[] = [];
    stream.on((e) => received.push(e));
    stream.connect();

    await vi.waitFor(() => expect(received).toHaveLength(1));
    stream.disconnect();
  });

  it('delivers several frames arriving in one chunk', async () => {
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValue(
          streamOf(
            [
              'id: 1\nevent: a\ndata: {"type":"a","seq":1,"ts":"t","data":{}}\n\n' +
                'id: 2\nevent: b\ndata: {"type":"b","seq":2,"ts":"t","data":{}}\n\n',
            ],
            { hang: true },
          ),
        ),
    );

    const stream = new EventStream(client());
    const received: unknown[] = [];
    stream.on((e) => received.push(e));
    stream.connect();

    await vi.waitFor(() => expect(received).toHaveLength(2));
    stream.disconnect();
  });

  it('ignores malformed JSON rather than tearing down the stream', async () => {
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValue(
          streamOf(
            [
              'id: 1\nevent: a\ndata: {not json\n\n',
              'id: 2\nevent: b\ndata: {"type":"b","seq":2,"ts":"t","data":{}}\n\n',
            ],
            { hang: true },
          ),
        ),
    );

    const stream = new EventStream(client());
    const received: unknown[] = [];
    stream.on((e) => received.push(e));
    stream.connect();

    await vi.waitFor(() => expect(received).toHaveLength(1));
    stream.disconnect();
  });

  it('resends Last-Event-ID when reconnecting', async () => {
    let call = 0;
    const fetchMock = vi.fn().mockImplementation(() => {
      call += 1;
      if (call === 1) {
        return Promise.resolve(
          streamOf(['id: 5\nevent: a\ndata: {"type":"a","seq":5,"ts":"t","data":{}}\n\n']),
        );
      }
      return Promise.resolve(streamOf([], { hang: true }));
    });
    vi.stubGlobal('fetch', fetchMock);

    const stream = new EventStream(client());
    stream.connect();
    await vi.waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThan(1), {
      timeout: 3000,
    });

    const init = fetchMock.mock.calls[1]?.[1] as RequestInit;
    expect((init.headers as Record<string, string>)['Last-Event-ID']).toBe('5');
    stream.disconnect();
  });

  it('unsubscribes cleanly', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        streamOf(['id: 1\nevent: a\ndata: {"type":"a","seq":1,"ts":"t","data":{}}\n\n'], {
          hang: true,
        }),
      ),
    );
    const stream = new EventStream(client());
    const received: unknown[] = [];
    const off = stream.on((e) => received.push(e));
    off();
    stream.connect();
    await new Promise((r) => setTimeout(r, 50));
    stream.disconnect();
    expect(received).toHaveLength(0);
  });

  it('surfaces a failed handshake as reconnecting, not silence', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({ ok: false, status: 401, body: null } as Response),
    );
    const stream = new EventStream(client());
    stream.connect();
    await vi.waitFor(() => expect(stream.state).toBe('reconnecting'), { timeout: 3000 });
    stream.disconnect();
  });
});
