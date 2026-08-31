/**
 * Handing a fetched blob to the browser as a file (OI-35).
 *
 * This exists because the export links could not stay links. A plain
 * `<a href download>` is a navigation, and a navigation carries no
 * Authorization header — so every export produced 401 and Chrome reported it
 * as "file was not available on the site", which names neither the cause nor
 * anything the user can do. Putting the token in the URL is forbidden outright
 * (SPEC-0 §9): it would land in history, in referrers and in the audit log.
 *
 * So the UI fetches with the header it already holds and saves the result
 * itself. It is the same repair OI-30 made for the module report link, which
 * was the same anchor making the same mistake one component over.
 */

/** Injected so a test can drive this without a real DOM download. */
export interface SaveDeps {
  createObjectURL: (blob: Blob) => string;
  revokeObjectURL: (url: string) => void;
  anchor: () => HTMLAnchorElement;
}

function defaultDeps(): SaveDeps {
  return {
    createObjectURL: (blob) => URL.createObjectURL(blob),
    revokeObjectURL: (url) => URL.revokeObjectURL(url),
    anchor: () => document.createElement('a'),
  };
}

/**
 * Save `blob` as `filename`.
 *
 * The object URL is revoked in a `finally`: it pins the blob in memory until
 * released, and a session export is measured in megabytes. Leaking one per
 * click would be invisible until the tab had been open a while, which is the
 * kind of bug that gets blamed on the daemon.
 */
export function saveBlob(blob: Blob, filename: string, deps: SaveDeps = defaultDeps()): void {
  const url = deps.createObjectURL(blob);
  try {
    const link = deps.anchor();
    link.href = url;
    link.download = filename;
    link.click();
  } finally {
    deps.revokeObjectURL(url);
  }
}

/**
 * The filename a `content-disposition` header asks for, or null.
 *
 * The daemon already sends one — `attachment; filename="<id>.<fmt>.json"` —
 * and honouring it keeps the name in one place rather than reconstructing it
 * here and letting the two drift.
 */
export function filenameFromDisposition(header: string | null): string | null {
  if (!header) return null;
  const match = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(header);
  const name = match?.[1]?.trim();
  if (!name) return null;
  // Never let a header choose a path. The daemon does not send one, but this
  // value reaches a `download` attribute, and a basename is all it may be.
  return name.replace(/^.*[\\/]/, '') || null;
}
