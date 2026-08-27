/**
 * Live event stream (SPEC-2 §4.3).
 *
 * Implemented on streaming fetch rather than EventSource. EventSource cannot
 * set request headers, so using it would force the bearer token into the query
 * string — where it would land in logs, history, and Referer. The token grants
 * read access to captured traffic, so that is not a trade worth making
 * (implementation-plan.md §2.5).
 *
 * What this costs us is reconnection, which EventSource does for free. That is
 * implemented below, with Last-Event-ID so the daemon can tell us whether we
 * missed anything while away (SPEC-0 §7.2).
 */
import type { ApiClient } from './client';
import type { FlowFilter, WireEvent } from './types';

export type StreamState = 'connecting' | 'open' | 'reconnecting' | 'closed';

export type EventHandler = (event: WireEvent) => void;
export type StateHandler = (state: StreamState) => void;

const INITIAL_BACKOFF_MS = 500;
const MAX_BACKOFF_MS = 10_000;

/** Parse one SSE frame. Comment-only frames (heartbeats) yield null. */
export function parseFrame(frame: string): { id?: string; event?: string; data?: string } | null {
  const out: { id?: string; event?: string; data?: string } = {};
  const dataLines: string[] = [];
  let sawField = false;

  for (const rawLine of frame.split('\n')) {
    if (rawLine === '' || rawLine.startsWith(':')) continue;
    const colon = rawLine.indexOf(':');
    const field = colon === -1 ? rawLine : rawLine.slice(0, colon);
    const value = colon === -1 ? '' : rawLine.slice(colon + 1).replace(/^ /, '');
    sawField = true;
    if (field === 'id') out.id = value;
    else if (field === 'event') out.event = value;
    else if (field === 'data') dataLines.push(value);
  }

  if (!sawField) return null;
  if (dataLines.length > 0) out.data = dataLines.join('\n');
  return out;
}

export class EventStream {
  private controller: AbortController | null = null;
  private handlers = new Set<EventHandler>();
  private stateHandlers = new Set<StateHandler>();
  private lastEventId: string | null = null;
  private backoff = INITIAL_BACKOFF_MS;
  private stopped = true;
  private currentState: StreamState = 'closed';

  constructor(private readonly api: ApiClient) {}

  get state(): StreamState {
    return this.currentState;
  }

  on(handler: EventHandler): () => void {
    this.handlers.add(handler);
    return () => this.handlers.delete(handler);
  }

  onState(handler: StateHandler): () => void {
    this.stateHandlers.add(handler);
    return () => this.stateHandlers.delete(handler);
  }

  private setState(state: StreamState): void {
    if (this.currentState === state) return;
    this.currentState = state;
    for (const handler of this.stateHandlers) handler(state);
  }

  connect(filter: FlowFilter = {}, kinds?: string[]): void {
    this.stopped = false;
    void this.loop(filter, kinds);
  }

  disconnect(): void {
    this.stopped = true;
    this.controller?.abort();
    this.controller = null;
    this.setState('closed');
  }

  private async loop(filter: FlowFilter, kinds?: string[]): Promise<void> {
    while (!this.stopped) {
      try {
        this.setState(this.lastEventId ? 'reconnecting' : 'connecting');
        await this.stream(filter, kinds);
        // A clean end means the server closed. Reconnect rather than going
        // quiet: a UI that stops updating without saying so is a UI that lies.
      } catch {
        // Network error or abort. Aborts are handled by the stopped flag.
      }
      if (this.stopped) break;
      this.setState('reconnecting');
      await new Promise((resolve) => setTimeout(resolve, this.backoff));
      this.backoff = Math.min(this.backoff * 2, MAX_BACKOFF_MS);
    }
    this.setState('closed');
  }

  private async stream(filter: FlowFilter, kinds?: string[]): Promise<void> {
    this.controller = new AbortController();
    const response = await fetch(this.api.eventsUrl(filter, kinds), {
      headers: this.api.streamHeaders(this.lastEventId),
      signal: this.controller.signal,
    });

    if (!response.ok || !response.body) {
      throw new Error(`event stream failed: ${response.status}`);
    }

    this.setState('open');
    this.backoff = INITIAL_BACKOFF_MS;

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      let boundary = buffer.indexOf('\n\n');
      while (boundary !== -1) {
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        this.dispatch(frame);
        boundary = buffer.indexOf('\n\n');
      }
    }
  }

  private dispatch(frame: string): void {
    const parsed = parseFrame(frame);
    if (!parsed?.data) return;
    if (parsed.id) this.lastEventId = parsed.id;

    let payload: WireEvent;
    try {
      payload = JSON.parse(parsed.data) as WireEvent;
    } catch {
      return;
    }
    for (const handler of this.handlers) handler(payload);
  }
}
