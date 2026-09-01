/**
 * The toolbar icon, with two status lights composited into it.
 *
 * The badge (badge.ts) answers "what happened" — counts, and the one-character
 * shout when something broke. It cannot answer the two questions a user of an
 * interception proxy asks constantly and pre-verbally:
 *
 *   is my traffic going through pporlock right now?
 *   is it being written to disk right now?
 *
 * Both are states, not events, and both are dangerous to be wrong about. A
 * badge showing `12` says nothing about either, and the badge can only show one
 * thing at a time — which is why these are drawn into the icon instead, where
 * they coexist with whatever the badge is saying.
 *
 * Two lamps, in the two top corners, so they never collide with the badge
 * (Chrome draws that across the bottom):
 *
 *   top-left    grey / green — whether the proxy is actually intercepting
 *   top-right   red, present only while recording
 *
 * The green lamp means *intercepting*, not *the user asked for interception*.
 * Those differ in every failure this extension has: a tripped fail-safe, a dead
 * daemon, a proxy setting another extension holds. In all of them the user
 * asked for the proxy and is not getting it, and a lamp that reported the
 * request rather than the reality would be green through every one of them.
 *
 * The recording lamp is drawn only while recording rather than being an unlit
 * dot the rest of the time: recording is the exceptional state, and a permanent
 * second dot is noise that trains the eye to stop seeing it.
 *
 * Everything here except `iconBitmap`/`renderIcon` is pure and drawn against a
 * two-method context interface, so the layout is unit-tested rather than
 * eyeballed in a browser.
 */

export interface IconLights {
  /** The proxy lamp. `on` means traffic really is going through the daemon. */
  proxy: 'on' | 'off';
  /** The recording lamp, drawn only when true. */
  recording: boolean;
}

export interface IconLightInput {
  /** What the user asked for. */
  proxyEnabled: boolean;
  /** Whether Chrome's proxy configuration is actually ours. */
  proxyApplied: boolean;
  daemonReachable: boolean;
  failSafeTripped: boolean;
  recording: boolean;
}

/**
 * Which lamps are lit.
 *
 * Every clause is a way the proxy can be "on" and not intercepting; each one
 * has been a real support question at some point in this project's life.
 */
export function iconLights(input: IconLightInput): IconLights {
  const intercepting =
    input.proxyEnabled && input.proxyApplied && input.daemonReachable && !input.failSafeTripped;
  return { proxy: intercepting ? 'on' : 'off', recording: input.recording };
}

/** Lamp colours. Chosen for the toolbar, which may be light or dark. */
export const LIGHT_COLORS = {
  /** Not intercepting. Grey, not absent: the lamp itself is the affordance. */
  proxyOff: '#8b949e',
  proxyOn: '#2ea043',
  recording: '#e5484d',
  /** Drawn under every lamp so it reads against a petal as well as background. */
  ring: '#1b1210',
} as const;

export interface LampPlacement {
  key: 'proxy' | 'recording';
  /** Centre and radius in device pixels, for a square icon of `size`. */
  x: number;
  y: number;
  r: number;
  fill: string;
  ring: string;
  ringWidth: number;
}

/**
 * Where the flower goes, as a fraction of the icon box.
 *
 * Inset and pushed down so the two top corners are background rather than
 * petal. Without this the lamps sit on red and the grey one disappears.
 */
export const FLOWER_BOX = { x: 0.08, y: 0.2, w: 0.84, h: 0.8 } as const;

/** Lamp geometry, as a fraction of the icon box. */
const LAMP = { r: 0.15, inset: 0.16 } as const;

/**
 * The lamps to draw, in draw order.
 *
 * Pure, and separate from the drawing, because the thing worth pinning is *what
 * is lit and where* — not which canvas calls that took.
 */
export function lampPlacements(size: number, lights: IconLights): LampPlacement[] {
  const r = size * LAMP.r;
  const y = size * LAMP.inset;
  // A hairline at 16px and still a hairline at 128: the ring exists to separate
  // the lamp from whatever is behind it, not to be a visible border.
  const ringWidth = Math.max(1, size * 0.03);

  const lamps: LampPlacement[] = [
    {
      key: 'proxy',
      x: size * LAMP.inset,
      y,
      r,
      fill: lights.proxy === 'on' ? LIGHT_COLORS.proxyOn : LIGHT_COLORS.proxyOff,
      ring: LIGHT_COLORS.ring,
      ringWidth,
    },
  ];
  if (lights.recording) {
    lamps.push({
      key: 'recording',
      x: size * (1 - LAMP.inset),
      y,
      r,
      fill: LIGHT_COLORS.recording,
      ring: LIGHT_COLORS.ring,
      ringWidth,
    });
  }
  return lamps;
}

/**
 * The subset of CanvasRenderingContext2D this needs.
 *
 * Declared rather than imported so a test can implement it in a dozen lines;
 * jsdom has no canvas, and an OffscreenCanvas exists in the service worker but
 * in no test environment this project runs.
 */
export interface IconContext {
  fillStyle: string;
  strokeStyle: string;
  lineWidth: number;
  clearRect(x: number, y: number, w: number, h: number): void;
  drawImage(image: unknown, dx: number, dy: number, dw: number, dh: number): void;
  beginPath(): void;
  arc(x: number, y: number, r: number, start: number, end: number): void;
  fill(): void;
  stroke(): void;
}

/** Draws the flower and its lamps into a `size`×`size` context. */
export function drawIcon(
  ctx: IconContext,
  size: number,
  flower: unknown,
  lights: IconLights,
): void {
  ctx.clearRect(0, 0, size, size);
  ctx.drawImage(
    flower,
    size * FLOWER_BOX.x,
    size * FLOWER_BOX.y,
    size * FLOWER_BOX.w,
    size * FLOWER_BOX.h,
  );
  for (const lamp of lampPlacements(size, lights)) {
    ctx.beginPath();
    ctx.arc(lamp.x, lamp.y, lamp.r, 0, Math.PI * 2);
    ctx.fillStyle = lamp.fill;
    ctx.fill();
    ctx.strokeStyle = lamp.ring;
    ctx.lineWidth = lamp.ringWidth;
    ctx.stroke();
  }
}

/** The sizes Chrome asks for in the toolbar. Both are supplied at once. */
export const ICON_SIZES = [16, 32] as const;

/** The artwork the lamps are composited onto. */
export const FLOWER_PNG = 'icons/poppy-128.png';

/**
 * A one-sentence description of what the lamps are saying, for the tooltip and
 * for the popup's own legend. The lamps are only useful if their meaning is
 * discoverable somewhere.
 */
export function describeLights(lights: IconLights): string {
  const proxy =
    lights.proxy === 'on' ? 'intercepting traffic' : 'not intercepting — traffic is going direct';
  return lights.recording ? `${proxy}; recording to disk` : proxy;
}

// -- the browser adapter ----------------------------------------------
//
// Everything below needs a real service-worker global. It is deliberately thin:
// fetch the artwork, draw, hand Chrome the pixels.

/** Decoded once per worker lifetime; MV3 will discard it soon enough anyway. */
let flowerBitmap: Promise<ImageBitmap> | null = null;

export function iconBitmap(url: string): Promise<ImageBitmap> {
  flowerBitmap ??= fetch(url)
    .then((response) => response.blob())
    .then((blob) => createImageBitmap(blob));
  return flowerBitmap;
}

/**
 * Composites the icon at every size Chrome wants and applies it.
 *
 * Failures are swallowed by the caller rather than here: an icon that could not
 * be drawn must never take down a badge refresh, which is carrying the more
 * important message.
 */
export async function renderIcon(lights: IconLights, tabId?: number): Promise<void> {
  const flower = await iconBitmap(chrome.runtime.getURL(FLOWER_PNG));
  const imageData: Record<number, ImageData> = {};
  for (const size of ICON_SIZES) {
    const canvas = new OffscreenCanvas(size, size);
    const ctx = canvas.getContext('2d');
    if (ctx === null) return;
    drawIcon(ctx as unknown as IconContext, size, flower, lights);
    // Numeric literal from a frozen tuple; no input reaches this key.
    // eslint-disable-next-line security/detect-object-injection
    imageData[size] = ctx.getImageData(0, 0, size, size);
  }
  await chrome.action.setIcon({ imageData, ...(tabId === undefined ? {} : { tabId }) });
}
