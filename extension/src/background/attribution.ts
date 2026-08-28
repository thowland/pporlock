/**
 * Tab attribution (SPEC-3 §6, REQ OI-2).
 *
 * Only the extension knows which tab a request came from. It observes at
 * `chrome.webRequest.onBeforeRequest` — observation only, no blocking; the
 * daemon does the intercepting — and posts batched associations to the control
 * API, which joins them against flows it has already seen.
 *
 * Three properties matter more than accuracy:
 *
 *  - **It never delays a request.** The listener records and returns.
 *  - **It does not depend on a timer.** An MV3 service worker can be suspended
 *    with a pending setTimeout, which silently loses the batch — observed
 *    directly during the OI-2 spike, where a 500 ms batch timer produced zero
 *    submissions. Batching is instead driven by round-trip time: a flush starts
 *    immediately, and anything arriving while it is in flight forms the next
 *    batch. A pending fetch also keeps the worker alive, so the batch it is
 *    carrying cannot be lost the same way.
 *  - **It is bounded.** A browser that fires faster than we can flush must not
 *    grow this buffer without limit; on overflow the oldest are dropped and
 *    counted, never queued indefinitely.
 *  - **Failure is silent to the user.** Attribution is a nicety; a daemon that
 *    rejects a batch must not produce an error state, because nothing the user
 *    can see is broken.
 */

/**
 * Retained for the deferred-flush path only. Batching is primarily driven by
 * round-trip time; see the note above.
 */
export const BATCH_INTERVAL_MS = 250;
export const BATCH_MAX = 50;
export const BUFFER_MAX = 2000;

export interface Observation {
  method: string;
  url: string;
  ts: number;
  tabId: number;
  frameId: number;
  type: string;
}

export interface AttributionSink {
  submit(entries: Observation[]): Promise<{ accepted: number } | void>;
}

export class Attributor {
  /**
   * Origin whose requests are never observed.
   *
   * Without this the attribution POST is itself observed, which schedules
   * another POST, which is observed in turn — a feedback loop that generates
   * traffic forever on an idle browser. Seen directly during the OI-2 spike.
   */
  private ignoreOrigin: string | null = null;

  private buffer: Observation[] = [];
  private timer: ReturnType<typeof setTimeout> | null = null;
  /**
   * One flush at a time.
   *
   * Without this the buffer drains synchronously on every batch-sized burst and
   * BUFFER_MAX is unreachable — but the *in-flight* requests accumulate
   * instead, which is the unbounded growth the cap was meant to prevent. With
   * it, a slow or hanging daemon backs pressure into the buffer, where the cap
   * can actually see it.
   */
  private flushing = false;
  private submittedCount = 0;
  private droppedCount = 0;
  private failedFlushes = 0;

  constructor(
    private readonly sink: AttributionSink,
    private readonly batchIntervalMs: number = BATCH_INTERVAL_MS,
    ignoreOrigin?: string,
  ) {
    this.ignoreOrigin = ignoreOrigin ?? null;
  }

  /** Requests to this origin are never observed. See ignoreOrigin. */
  setIgnoreOrigin(origin: string | null): void {
    this.ignoreOrigin = origin;
  }

  get pending(): number {
    return this.buffer.length;
  }

  get submitted(): number {
    return this.submittedCount;
  }

  get dropped(): number {
    return this.droppedCount;
  }

  get failures(): number {
    return this.failedFlushes;
  }

  /**
   * Record one observation.
   *
   * Only requests belonging to a real tab are worth recording: tabId is -1 for
   * requests the browser itself makes, and those have no tab to attribute to.
   */
  observe(details: {
    method: string;
    url: string;
    timeStamp: number;
    tabId: number;
    frameId?: number;
    type?: string;
  }): void {
    if (details.tabId < 0) return;
    if (!details.url.startsWith('http')) return;
    // Never attribute our own control-API traffic: doing so feeds the batch
    // that produced it and the loop never ends.
    if (this.ignoreOrigin !== null && details.url.startsWith(this.ignoreOrigin)) return;

    this.buffer.push({
      method: details.method,
      url: details.url,
      ts: details.timeStamp,
      tabId: details.tabId,
      frameId: details.frameId ?? 0,
      type: details.type ?? '',
    });

    if (this.buffer.length > BUFFER_MAX) {
      // Drop oldest. Attribution is best-effort and must never be a source of
      // memory growth or latency.
      const overflow = this.buffer.length - BUFFER_MAX;
      this.buffer.splice(0, overflow);
      this.droppedCount += overflow;
    }

    // Flush at once when idle. Waiting for a timer risks the worker being
    // suspended with the batch still buffered.
    if (!this.flushing) {
      void this.flush();
      return;
    }
    // A flush is in flight; this observation joins the next batch. The timer is
    // a backstop for the case where that flush is the last thing to happen.
    this.schedule();
  }

  private schedule(): void {
    if (this.timer !== null) return;
    this.timer = setTimeout(() => {
      this.timer = null;
      void this.flush();
    }, this.batchIntervalMs);
  }

  /** Send whatever is buffered. Never throws. */
  async flush(): Promise<number> {
    if (this.timer !== null) {
      clearTimeout(this.timer);
      this.timer = null;
    }
    if (this.buffer.length === 0 || this.flushing) return 0;

    this.flushing = true;
    const batch = this.buffer.splice(0, BATCH_MAX);
    try {
      await this.sink.submit(batch);
      this.submittedCount += batch.length;
      return batch.length;
    } catch {
      // Nothing the user can see is broken, so this stays silent. The counter
      // is surfaced in options diagnostics for when someone goes looking.
      this.failedFlushes += 1;
      this.droppedCount += batch.length;
      return 0;
    } finally {
      this.flushing = false;
      // More arrived while we were away; keep draining.
      if (this.buffer.length > 0) this.schedule();
    }
  }

  stop(): void {
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
    this.buffer = [];
    this.flushing = false;
  }
}
