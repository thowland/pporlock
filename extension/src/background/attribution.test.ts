/**
 * Tab attribution. SPEC-3 §6, REQ OI-2.
 *
 * The properties that matter here are not accuracy — attribution is
 * best-effort — but that it never delays a request, never grows without bound,
 * and never surfaces a failure the user cannot act on.
 */
import { describe, expect, it, vi } from 'vitest';
import { Attributor, BATCH_MAX, BUFFER_MAX, type Observation } from './attribution';

function sink() {
  const batches: Observation[][] = [];
  return {
    batches,
    submit: vi.fn(async (entries: Observation[]) => {
      batches.push(entries);
    }),
  };
}

function detail(overrides: Partial<Parameters<Attributor['observe']>[0]> = {}) {
  return {
    method: 'GET',
    url: 'https://a.example/x',
    timeStamp: Date.now(),
    tabId: 7,
    frameId: 0,
    type: 'script',
    ...overrides,
  };
}

describe('Attributor', () => {
  it('submits an observation without waiting for a timer', async () => {
    // An MV3 worker can be suspended with a pending setTimeout, which loses the
    // batch. Flushing immediately when idle is what makes attribution work at
    // all — a 500 ms timer produced zero submissions in the OI-2 spike.
    const target = sink();
    const attributor = new Attributor(target, 60_000);
    attributor.observe(detail());
    await vi.waitFor(() => expect(target.submit).toHaveBeenCalled());
    attributor.stop();
  });

  it('buffers while a flush is in flight, forming the next batch', () => {
    const attributor = new Attributor({ submit: () => new Promise<void>(() => {}) }, 60_000);
    attributor.observe(detail({ url: 'https://a.example/1' }));
    attributor.observe(detail({ url: 'https://a.example/2' }));
    expect(attributor.pending).toBe(1);
    attributor.stop();
  });

  it('ignores requests with no tab', () => {
    // tabId is -1 for requests the browser itself makes; there is nothing to
    // attribute them to.
    const attributor = new Attributor(sink());
    attributor.observe(detail({ tabId: -1 }));
    expect(attributor.pending).toBe(0);
  });

  it('ignores non-http schemes', () => {
    const attributor = new Attributor(sink());
    attributor.observe(detail({ url: 'chrome-extension://abc/page.html' }));
    expect(attributor.pending).toBe(0);
  });

  it('flushes on the batch size without waiting for the timer', async () => {
    const target = sink();
    const attributor = new Attributor(target, 60_000);
    for (let i = 0; i < BATCH_MAX; i += 1) {
      attributor.observe(detail({ url: `https://a.example/${i}` }));
    }
    await vi.waitFor(() => expect(target.submit).toHaveBeenCalled());
    attributor.stop();
  });

  it('flushes on the timer', async () => {
    const target = sink();
    const attributor = new Attributor(target, 10);
    attributor.observe(detail());
    await vi.waitFor(() => expect(target.submit).toHaveBeenCalled());
    attributor.stop();
  });

  it('sends the fields the daemon joins on', async () => {
    const target = sink();
    const attributor = new Attributor(target, 10);
    attributor.observe(detail({ method: 'POST', url: 'https://a.example/y', tabId: 3 }));
    await vi.waitFor(() => expect(target.batches).toHaveLength(1));
    expect(target.batches[0]?.[0]).toMatchObject({
      method: 'POST',
      url: 'https://a.example/y',
      tabId: 3,
    });
    attributor.stop();
  });

  describe('a hanging daemon', () => {
    /** Never resolves, so pressure backs into the buffer where the cap sees it. */
    const hanging = () => ({ submit: vi.fn(() => new Promise<void>(() => {})) });

    it('caps the buffer rather than growing without limit', () => {
      const attributor = new Attributor(hanging(), 60_000);
      for (let i = 0; i < BUFFER_MAX + 200; i += 1) {
        attributor.observe(detail({ url: `https://a.example/${i}` }));
      }
      expect(attributor.pending).toBeLessThanOrEqual(BUFFER_MAX);
      attributor.stop();
    });

    it('counts what it dropped rather than dropping silently', () => {
      const attributor = new Attributor(hanging(), 60_000);
      for (let i = 0; i < BUFFER_MAX + 200; i += 1) {
        attributor.observe(detail({ url: `https://a.example/${i}` }));
      }
      expect(attributor.dropped).toBeGreaterThan(0);
      attributor.stop();
    });

    it('does not start a second flush while one is in flight', () => {
      // Otherwise the buffer drains but the in-flight requests accumulate,
      // which is the unbounded growth the cap was meant to prevent.
      const target = hanging();
      const attributor = new Attributor(target, 60_000);
      for (let i = 0; i < BATCH_MAX * 5; i += 1) {
        attributor.observe(detail({ url: `https://a.example/${i}` }));
      }
      expect(target.submit).toHaveBeenCalledTimes(1);
      attributor.stop();
    });
  });

  it('counts what it submitted', async () => {
    const target = sink();
    const attributor = new Attributor(target, 10);
    attributor.observe(detail());
    await vi.waitFor(() => expect(attributor.submitted).toBe(1));
    attributor.stop();
  });

  it('a failed flush never throws into the caller', async () => {
    // Attribution is a nicety. A daemon that rejects a batch must not produce
    // an error state, because nothing the user can see is broken.
    const failing = { submit: vi.fn().mockRejectedValue(new Error('daemon down')) };
    const attributor = new Attributor(failing, 10);
    attributor.observe(detail());
    await expect(attributor.flush()).resolves.toBe(0);
    expect(attributor.failures).toBe(1);
    attributor.stop();
  });

  it('flushing an empty buffer is a no-op', async () => {
    const target = sink();
    expect(await new Attributor(target).flush()).toBe(0);
    expect(target.submit).not.toHaveBeenCalled();
  });

  it('stop clears the buffer and the timer', () => {
    const attributor = new Attributor(sink(), 60_000);
    attributor.observe(detail());
    attributor.stop();
    expect(attributor.pending).toBe(0);
  });

  it('splits a large backlog across flushes rather than one huge post', async () => {
    const target = sink();
    const attributor = new Attributor(target, 60_000);
    for (let i = 0; i < BATCH_MAX * 2; i += 1) {
      attributor.observe(detail({ url: `https://a.example/${i}` }));
    }
    await vi.waitFor(() => expect(target.batches.length).toBeGreaterThan(0));
    expect(target.batches[0]?.length).toBeLessThanOrEqual(BATCH_MAX);
    attributor.stop();
  });
});

describe('Attributor — self-observation', () => {
  it('never observes its own control-API traffic', () => {
    // Otherwise the attribution POST is observed, which schedules another POST,
    // which is observed in turn: a loop that generates traffic forever on an
    // idle browser. Seen directly during the OI-2 spike.
    const target = sink();
    const attributor = new Attributor(target, 60_000, 'http://127.0.0.1:8081');
    attributor.observe(detail({ url: 'http://127.0.0.1:8081/attribution' }));
    expect(attributor.pending).toBe(0);
    expect(target.submit).not.toHaveBeenCalled();
    attributor.stop();
  });

  it('still observes ordinary traffic', () => {
    const attributor = new Attributor(
      { submit: () => new Promise<void>(() => {}) },
      60_000,
      'http://127.0.0.1:8081',
    );
    attributor.observe(detail({ url: 'https://a.example/x' }));
    attributor.observe(detail({ url: 'https://a.example/y' }));
    expect(attributor.pending).toBeGreaterThan(0);
    attributor.stop();
  });

  it('follows a control-origin change', () => {
    const target = sink();
    const attributor = new Attributor(target, 60_000, 'http://127.0.0.1:8081');
    attributor.setIgnoreOrigin('http://127.0.0.1:9999');
    attributor.observe(detail({ url: 'http://127.0.0.1:9999/attribution' }));
    expect(attributor.pending).toBe(0);
    attributor.stop();
  });
});
