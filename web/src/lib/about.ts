/**
 * Who this is, where it came from, and where its documentation lives.
 *
 * Deliberately duplicated from `extension/src/shared/about.ts` rather than
 * shared: the two are separate builds with separate lifetimes, and the one
 * mechanism this repository has for crossing that boundary — `contracts/` — is
 * for wire shapes, not for prose. What is *not* left to chance is the doc
 * links: `about.test.ts` checks every one of them against the files actually in
 * the repository, because a help link to a renamed document is a 404 delivered
 * to someone who is already stuck.
 */

export const PROJECT_NAME = 'pporlock';
export const HOMEPAGE = 'https://github.com/thowland/pporlock';
export const ISSUES = `${HOMEPAGE}/issues`;
export const LICENSE = 'GPL-3.0-or-later';
export const LICENSE_URL = 'https://www.gnu.org/licenses/gpl-3.0.html';
export const COPYRIGHT = '© 2025 Tim Howland';

export const SUMMARY =
  'pporlock is a local HTTPS interception proxy for a single machine. It decrypts, ' +
  'inspects and can rewrite your browser’s traffic using modules you write yourself, ' +
  'so you can see and change what a site sends and receives.';

export const TRUST_NOTE =
  'Modules run as ordinary Python, unsandboxed, with full access to intercepted ' +
  'traffic — including during a dry run. Only enable modules you have read.';

/** Where a doc lives on GitHub. `master`, because releases are not tagged. */
export function docUrl(file: string): string {
  return `${HOMEPAGE}/blob/master/${file}`;
}

export interface Guide {
  /** Repository-relative path. Checked against the working tree by the test. */
  file: string;
  title: string;
  /** What it is for, in the terms of someone deciding whether to open it. */
  blurb: string;
}

/**
 * The module-authoring documentation, in the order someone meets it.
 *
 * Ordered as a path rather than alphabetically: a tutorial, then the recipes,
 * then the reference. Someone who has never written a module and opens the
 * reference first concludes the system is harder than it is.
 */
export const GUIDES: Guide[] = [
  {
    file: 'docs/tutorial-declarative-module.md',
    title: 'Tutorial — a declarative module',
    blurb:
      'Start here. Builds a working module out of YAML rules alone, with no Python at all: ' +
      'match a request, change it, watch the provenance say what happened.',
  },
  {
    file: 'docs/tutorial-python-module.md',
    title: 'Tutorial — a Python module',
    blurb:
      'The next step, for anything rules cannot express. Covers the hook signatures, what a ' +
      'hook may return, and how an exception is contained rather than breaking your browsing.',
  },
  {
    file: 'docs/module-cookbook.md',
    title: 'Cookbook',
    blurb:
      'Worked recipes for the things people actually want: blocking a tracker, faking a slow ' +
      'response, rewriting JSON in flight, serving a local file in place of a remote one.',
  },
  {
    file: 'docs/module-authoring.md',
    title: 'Authoring reference',
    blurb:
      'The complete module contract — manifest fields, the settings schema, asset containment, ' +
      'priority and ordering, and the full list of what a hook may do.',
  },
  {
    file: 'docs/rule-schema.md',
    title: 'Rule schema',
    blurb:
      'Every match condition and every action a declarative rule can use, generated from the ' +
      'JSON Schema the daemon validates against — so it cannot drift from what is enforced.',
  },
  {
    file: 'docs/worked-example.md',
    title: 'Worked example',
    blurb:
      'One problem carried end to end: record a session, find the flow that broke, derive a ' +
      'rule from it, dry-run it against the recording, then enable it.',
  },
];

/** Documentation reached from the help view rather than the modules page. */
export const HELP_DOCS: Guide[] = [
  {
    file: 'docs/install.md',
    title: 'Installing',
    blurb: 'Toolchain, the CA certificate, the launch agent, and pairing the extension.',
  },
  {
    file: 'docs/troubleshooting.md',
    title: 'Troubleshooting',
    blurb: 'When the proxy is on and nothing is intercepted, and the other usual failures.',
  },
  {
    file: 'docs/llm-with-mcp.md',
    title: 'Authoring modules with an LLM',
    blurb: 'What the MCP server exposes, and the two things it deliberately cannot do.',
  },
  {
    file: 'SECURITY.md',
    title: 'Security model',
    blurb: 'What is a boundary and what is not — in particular, why module code is trusted.',
  },
];

/**
 * The colours of the two lamps the extension draws into its toolbar icon.
 *
 * Mirrored from `extension/src/background/icon.ts` so the help's legend looks
 * like the thing it is describing. A legend whose colours are merely nearby is
 * worse than none: it teaches the wrong green.
 */
export const LAMP_COLORS = {
  proxyOn: '#2ea043',
  proxyOff: '#8b949e',
  recording: '#e5484d',
} as const;
