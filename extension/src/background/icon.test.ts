/**
 * The icon lamps (REQ EXT-012).
 *
 * Two things are worth pinning here, and neither is "the canvas was called":
 *
 *   1. The green lamp means *intercepting*, so every way the proxy can be
 *      requested-but-not-working must produce grey. This is the whole reason
 *      the lamp is not simply `state.proxyEnabled`, and it is exactly the sort
 *      of condition that quietly loses a clause during a refactor.
 *   2. The lamps sit in the top corners, clear of the badge. A lamp drawn under
 *      the badge is invisible, which is indistinguishable from a lamp that is
 *      off — the worst failure an indicator can have.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  drawIcon,
  describeLights,
  FLOWER_BOX,
  iconLights,
  ICON_SIZES,
  lampPlacements,
  LIGHT_COLORS,
  type IconContext,
  type IconLights,
} from './icon';

const INTERCEPTING = {
  proxyEnabled: true,
  proxyApplied: true,
  daemonReachable: true,
  failSafeTripped: false,
  recording: false,
};

describe('iconLights', () => {
  it('is green only when traffic really is going through the daemon', () => {
    expect(iconLights(INTERCEPTING).proxy).toBe('on');
  });

  it.each([
    ['the user has not turned it on', { proxyEnabled: false }],
    ['Chrome’s proxy setting is not ours', { proxyApplied: false }],
    ['the daemon is not answering', { daemonReachable: false }],
    ['the fail-safe returned Chrome to a direct connection', { failSafeTripped: true }],
  ])('is grey when %s', (_why, patch) => {
    // Each of these is a state in which the user asked for the proxy and is not
    // getting it. A lamp reporting the request rather than the reality would be
    // green through all four.
    expect(iconLights({ ...INTERCEPTING, ...patch }).proxy).toBe('off');
  });

  it('reports recording independently of the proxy lamp', () => {
    // Recording is a daemon-side fact: it keeps running through a fail-safe
    // trip, and saying otherwise would understate what is on disk.
    const lights = iconLights({ ...INTERCEPTING, proxyEnabled: false, recording: true });
    expect(lights).toEqual({ proxy: 'off', recording: true });
  });
});

describe('lampPlacements', () => {
  it('always shows the proxy lamp, so its absence is never ambiguous', () => {
    for (const proxy of ['on', 'off'] as const) {
      const lamps = lampPlacements(32, { proxy, recording: false });
      expect(lamps.map((l) => l.key)).toEqual(['proxy']);
    }
  });

  it('adds the recording lamp only while recording', () => {
    expect(lampPlacements(32, { proxy: 'on', recording: true }).map((l) => l.key)).toEqual([
      'proxy',
      'recording',
    ]);
  });

  it('uses grey for a proxy that is off and green for one that is on', () => {
    const fill = (proxy: IconLights['proxy']) =>
      lampPlacements(32, { proxy, recording: false })[0]?.fill;
    expect(fill('off')).toBe(LIGHT_COLORS.proxyOff);
    expect(fill('on')).toBe(LIGHT_COLORS.proxyOn);
    // Colour is never the only difference elsewhere in this extension, and it
    // is not here either: position tells the two lamps apart.
    expect(fill('on')).not.toBe(fill('off'));
  });

  it.each(ICON_SIZES)('keeps both lamps in the top half at %ipx, clear of the badge', (size) => {
    const lamps = lampPlacements(size, { proxy: 'on', recording: true });
    for (const lamp of lamps) {
      expect(lamp.y + lamp.r).toBeLessThanOrEqual(size / 2);
      expect(lamp.x - lamp.r).toBeGreaterThanOrEqual(0);
      expect(lamp.x + lamp.r).toBeLessThanOrEqual(size);
    }
  });

  it('puts the proxy lamp left and the recording lamp right', () => {
    const [proxy, recording] = lampPlacements(32, { proxy: 'on', recording: true });
    expect(proxy?.x).toBeLessThan(recording?.x ?? 0);
  });

  it('gives every lamp a ring at least one pixel wide, even at 16px', () => {
    // The ring is what separates a lamp from the petal behind it. A sub-pixel
    // stroke is a stroke Chrome will not draw.
    for (const size of ICON_SIZES) {
      for (const lamp of lampPlacements(size, { proxy: 'off', recording: true })) {
        expect(lamp.ringWidth).toBeGreaterThanOrEqual(1);
      }
    }
  });
});

/** Records the drawing calls in order, with the style in force at each. */
function fakeContext() {
  const calls: string[] = [];
  const ctx: IconContext & { calls: string[] } = {
    calls,
    fillStyle: '',
    strokeStyle: '',
    lineWidth: 0,
    clearRect: (x, y, w, h) => calls.push(`clear ${x},${y},${w},${h}`),
    drawImage: (_image, dx, dy, dw, dh) => calls.push(`image ${dx},${dy},${dw},${dh}`),
    beginPath: () => calls.push('begin'),
    arc: (x, y, r) => calls.push(`arc ${x},${y},${r}`),
    fill: () => calls.push(`fill ${ctx.fillStyle}`),
    stroke: () => calls.push(`stroke ${ctx.strokeStyle}`),
  };
  return ctx;
}

describe('drawIcon', () => {
  it('clears first, so a lamp that went out actually goes out', () => {
    const ctx = fakeContext();
    drawIcon(ctx, 32, {}, { proxy: 'off', recording: false });
    expect(ctx.calls[0]).toBe('clear 0,0,32,32');
  });

  it('draws the flower inset and pushed down, leaving the corners for the lamps', () => {
    const ctx = fakeContext();
    drawIcon(ctx, 100, {}, { proxy: 'on', recording: false });
    expect(ctx.calls[1]).toBe(
      `image ${100 * FLOWER_BOX.x},${100 * FLOWER_BOX.y},${100 * FLOWER_BOX.w},${100 * FLOWER_BOX.h}`,
    );
    // The offset exists so the lamps land on the artwork's transparent corners
    // rather than on a petal — a grey lamp on a red petal is not grey. So each
    // lamp must sit in a corner: outside the middle third horizontally, and
    // entirely within the top third vertically.
    for (const lamp of lampPlacements(100, { proxy: 'on', recording: true })) {
      expect(lamp.x < 100 / 3 || lamp.x > (100 * 2) / 3).toBe(true);
      expect(lamp.y + lamp.r).toBeLessThanOrEqual(100 / 3);
    }
  });

  it('fills and rings each lamp', () => {
    const ctx = fakeContext();
    drawIcon(ctx, 32, {}, { proxy: 'on', recording: true });
    expect(ctx.calls.filter((c) => c.startsWith('fill '))).toEqual([
      `fill ${LIGHT_COLORS.proxyOn}`,
      `fill ${LIGHT_COLORS.recording}`,
    ]);
    expect(ctx.calls.filter((c) => c.startsWith('stroke '))).toEqual([
      `stroke ${LIGHT_COLORS.ring}`,
      `stroke ${LIGHT_COLORS.ring}`,
    ]);
  });
});

describe('describeLights', () => {
  it('says what the grey lamp means for the user’s browsing, not what it is', () => {
    expect(describeLights({ proxy: 'off', recording: false })).toContain('going direct');
  });

  it('mentions the disk when recording, because that is the consequence', () => {
    expect(describeLights({ proxy: 'on', recording: true })).toContain('recording to disk');
  });

  it('says nothing about recording when not recording', () => {
    expect(describeLights({ proxy: 'on', recording: false })).not.toContain('recording');
  });
});

/**
 * The browser adapter.
 *
 * Thin, but not too thin to be wrong in a way that matters: Chrome takes the
 * icon as an `imageData` map keyed by size, and supplying only one size gets a
 * blurry icon on half the world's displays. The artwork is also fetched by URL
 * — a string the bundler cannot see — and decoded once per worker lifetime,
 * which is the sort of cache that is easy to write and easy to write wrong.
 *
 * Each case re-imports the module so the cache starts empty; it lives at module
 * scope, which is correct for a service worker and would otherwise leak here.
 */
describe('renderIcon', () => {
  interface Recorded {
    setIcon: ReturnType<typeof vi.fn>;
    fetches: string[];
    decodes: number;
    sizes: number[];
  }

  async function withFakeWorker(): Promise<{ mod: typeof import('./icon'); rec: Recorded }> {
    vi.resetModules();
    const rec: Recorded = { setIcon: vi.fn(), fetches: [], decodes: 0, sizes: [] };
    vi.stubGlobal('fetch', (url: string) => {
      rec.fetches.push(url);
      return Promise.resolve({ blob: () => Promise.resolve({}) });
    });
    vi.stubGlobal('createImageBitmap', () => {
      rec.decodes += 1;
      return Promise.resolve({});
    });
    vi.stubGlobal(
      'OffscreenCanvas',
      class {
        constructor(readonly width: number) {
          rec.sizes.push(width);
        }
        getContext() {
          return {
            fillStyle: '',
            strokeStyle: '',
            lineWidth: 0,
            clearRect: () => {},
            drawImage: () => {},
            beginPath: () => {},
            arc: () => {},
            fill: () => {},
            stroke: () => {},
            getImageData: () => ({ width: this.width }),
          };
        }
      },
    );
    vi.stubGlobal('chrome', {
      runtime: { getURL: (path: string) => `chrome-extension://pporlock/${path}` },
      action: { setIcon: rec.setIcon },
    });
    return { mod: await import('./icon'), rec };
  }

  afterEach(() => vi.unstubAllGlobals());

  it('hands Chrome pixels for every size it asks for', async () => {
    const { mod, rec } = await withFakeWorker();
    await mod.renderIcon({ proxy: 'on', recording: false });
    expect(rec.sizes).toEqual([...mod.ICON_SIZES]);
    const [details] = rec.setIcon.mock.calls[0] as [{ imageData: Record<number, unknown> }];
    // Supplying only one size gets a blurry icon on every display that wanted
    // the other one.
    expect(Object.keys(details.imageData).map(Number)).toEqual([...mod.ICON_SIZES]);
  });

  it('fetches the artwork the manifest ships', async () => {
    const { mod, rec } = await withFakeWorker();
    await mod.renderIcon({ proxy: 'off', recording: false });
    expect(rec.fetches).toEqual([`chrome-extension://pporlock/${mod.FLOWER_PNG}`]);
  });

  it('decodes the artwork once, however often the lamps change', async () => {
    const { mod, rec } = await withFakeWorker();
    await mod.renderIcon({ proxy: 'off', recording: false });
    await mod.renderIcon({ proxy: 'on', recording: true });
    await mod.renderIcon({ proxy: 'on', recording: false });
    // Every badge refresh redraws the icon, and a refresh happens on every
    // health check. Re-fetching and re-decoding a PNG each time would be a
    // steady cost for no gain.
    expect(rec.decodes).toBe(1);
    expect(rec.setIcon).toHaveBeenCalledTimes(3);
  });

  it('scopes the icon to a tab when it has one', async () => {
    const { mod, rec } = await withFakeWorker();
    await mod.renderIcon({ proxy: 'on', recording: false }, 7);
    expect((rec.setIcon.mock.calls[0] as [{ tabId?: number }])[0].tabId).toBe(7);
  });

  it('sets the icon globally when it does not', async () => {
    const { mod, rec } = await withFakeWorker();
    await mod.renderIcon({ proxy: 'on', recording: false });
    // `tabId: undefined` is not the same as omitting it — Chrome rejects the
    // former rather than treating it as global.
    expect('tabId' in (rec.setIcon.mock.calls[0] as [object])[0]).toBe(false);
  });
});
