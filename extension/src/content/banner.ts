/**
 * In-page modification warning (SPEC-3 §8, REQ EXT-020).
 *
 * Stripping SRI, relaxing CSP, and injecting scripts all weaken a page's own
 * protections. A tool that does that invisibly is a tool that will eventually
 * surprise its user badly — most likely while they are debugging something
 * else and have forgotten a rule is active.
 *
 * The banner therefore names what was done and which module did it, and can be
 * suppressed per host. Suppression silences the banner, not the fact: the badge
 * and the DevTools panel still report it.
 *
 * Rendered in a **closed** shadow root with an all-properties reset, so page CSS
 * cannot hide or restyle it and nothing here leaks into the page. It injects
 * nothing else, listens to no page events, and never reads page content.
 */

export interface BannerNote {
  code: string;
  severity: string;
  module?: string | null;
  message?: string;
}

export interface BannerPayload {
  flowId: string;
  notes: BannerNote[];
  profile: string;
}

const HOST_ID = 'pporlock-banner-host';

/** What each note means to someone looking at the page it happened to. */
const MEANING: Record<string, string> = {
  sri_stripped: 'removed subresource-integrity attributes from this page',
  csp_modified: "changed this page's Content-Security-Policy",
  script_injected: 'injected a script into this page',
  dev_toggle_active:
    'is running with a development toggle on, so this page does not reflect normal behaviour',
  module_error: 'hit an error while handling this page',
  module_quarantined: 'disabled a module after repeated failures',
  transform_budget_exceeded: 'ran out of time and skipped a transform on this page',
  map_local_missing: 'could not find a local file a rule pointed at',
};

/** Only these are worth interrupting someone for. */
export function bannerWorthy(notes: BannerNote[]): BannerNote[] {
  return notes.filter((note) => note.severity === 'warning' || note.severity === 'error');
}

export function describe(notes: BannerNote[]): string[] {
  return notes.map((note) => {
    const what = MEANING[note.code] ?? note.code;
    return note.module ? `${note.module} ${what}` : `pporlock ${what}`;
  });
}

const STYLE = `
  :host { all: initial; }
  .wrap {
    all: initial;
    position: fixed; inset: 0 0 auto 0; z-index: 2147483647;
    font: 13px/1.5 -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #2b2113; color: #f0d9a8;
    border-bottom: 2px solid #e8b862;
    padding: 9px 14px; display: flex; gap: 12px; align-items: flex-start;
    box-shadow: 0 2px 12px rgba(0,0,0,.35);
  }
  .body { flex: 1; }
  .title { font-weight: 600; color: #ffd98a; }
  ul { margin: 4px 0 0; padding-left: 18px; }
  li { margin: 1px 0; }
  button {
    all: initial; cursor: pointer; font: inherit; font-size: 12px;
    color: #ffd98a; text-decoration: underline; padding: 2px 4px;
  }
  button:hover { color: #fff; }
  .actions { display: flex; gap: 10px; flex: 0 0 auto; }
`;

/**
 * The banner's visible content, separate from the shadow root that hosts it.
 *
 * Split out because a closed shadow root is, by design, unreachable from
 * outside — including from a test. Keeping the markup in its own function means
 * the accessible name, the role, and the escaping can all be asserted directly,
 * rather than trusted because they were written carefully.
 */
export function buildContent(
  notes: BannerNote[],
  actions: { dismiss: () => void; suppressHost: () => void },
  doc: Document,
  onRemove: () => void,
): HTMLElement {
  const wrap = doc.createElement('div');
  wrap.className = 'wrap';
  // A warning that a screen reader never announces is a warning that did not
  // happen. `alert` rather than `status` because this is not a progress
  // update — the page in front of you is not the page the site sent.
  wrap.setAttribute('role', 'alert');
  wrap.setAttribute('aria-label', 'pporlock modified this page');

  const body = doc.createElement('div');
  body.className = 'body';

  const title = doc.createElement('div');
  title.className = 'title';
  title.textContent = 'pporlock modified this page';

  const list = doc.createElement('ul');
  for (const line of describe(notes)) {
    const item = doc.createElement('li');
    item.textContent = line;
    list.appendChild(item);
  }

  body.append(title, list);

  const dismiss = doc.createElement('button');
  dismiss.textContent = 'dismiss';
  dismiss.addEventListener('click', () => {
    onRemove();
    actions.dismiss();
  });

  const suppress = doc.createElement('button');
  suppress.textContent = "don't warn for this host";
  suppress.addEventListener('click', () => {
    onRemove();
    actions.suppressHost();
  });

  const buttons = doc.createElement('div');
  buttons.className = 'actions';
  buttons.append(dismiss, suppress);

  wrap.append(body, buttons);
  return wrap;
}

export function buildBanner(
  payload: BannerPayload,
  actions: { dismiss: () => void; suppressHost: () => void },
  doc: Document = document,
): HTMLElement | null {
  const notes = bannerWorthy(payload.notes);
  if (notes.length === 0) return null;

  const existing = doc.getElementById(HOST_ID);
  if (existing) existing.remove();

  const host = doc.createElement('div');
  host.id = HOST_ID;
  // Closed: page script cannot reach in to hide or read it, and our styles
  // cannot leak out.
  const shadow = host.attachShadow({ mode: 'closed' });

  const style = doc.createElement('style');
  style.textContent = STYLE;

  shadow.append(
    style,
    buildContent(notes, actions, doc, () => host.remove()),
  );
  return host;
}

/**
 * Handle one message from the service worker.
 *
 * Separate from the listener registration so it can be driven directly: a
 * content script's message path is otherwise only reachable inside a real
 * extension context, which is precisely where a mistake is most expensive to
 * find.
 */
export function handleBannerMessage(message: unknown, doc: Document = document): boolean {
  const payload = message as { type?: string } & BannerPayload;
  if (payload?.type !== 'pporlock_banner') return false;

  const banner = buildBanner(
    payload,
    {
      dismiss: () => {},
      suppressHost: () => {
        void chrome.runtime.sendMessage({
          type: 'suppress_host',
          host: doc.location.hostname,
        });
      },
    },
    doc,
  );
  if (banner === null) return false;
  doc.documentElement.appendChild(banner);
  return true;
}

/** Only run in a real page context; the module is also imported by tests. */
if (typeof chrome !== 'undefined' && chrome.runtime?.onMessage) {
  chrome.runtime.onMessage.addListener((message: unknown) => {
    handleBannerMessage(message);
  });
}
