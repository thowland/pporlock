/**
 * The rule builder's form model (SPEC-2 §7.3, REQ WUI-007).
 *
 * `RuleDraft` is a *lossless projection* of SPEC-0 §5.3/§5.4, not a second
 * representation of it. Two properties matter and are tested:
 *
 *  1. `draftToRule(ruleToDraft(r))` deep-equals `r` for any rule the schema
 *     allows — including keys this form does not model, which survive in
 *     `extra` rather than being silently dropped.
 *  2. Shape choices YAML can express two ways (`method: GET` vs
 *     `method: [GET]`) are remembered, so opening a rule in the builder and
 *     closing it produces no diff.
 *
 * Everything the form cannot express lands in `extra` and is shown to the user
 * as raw YAML. Silent loss of a key the user wrote is the one failure mode a
 * generator like this must not have.
 */
import { stringifyRule } from './module-yaml';
import { parseDocument } from 'yaml';
import type { Match, Rule } from '../api/types';

export type RuleAction = 'passthrough' | 'block' | 'map_local' | 'redirect' | 'headers' | 'body';

export const RULE_ACTIONS: RuleAction[] = [
  'passthrough',
  'block',
  'map_local',
  'redirect',
  'headers',
  'body',
];

/** A key/value row. `presenceOnly` maps to a null value (header presence test). */
export interface Pair {
  key: string;
  value: string;
  presenceOnly?: boolean;
}

/**
 * `list` records whether the source spelled this as a YAML sequence, so a
 * single-element list stays a single-element list.
 */
export interface MultiField {
  text: string;
  list: boolean;
}

export interface MatchDraft {
  host: string;
  path: string;
  method: MultiField;
  dest: MultiField;
  status: MultiField;
  content_type: string;
  query: Pair[];
  request_headers: Pair[];
  /** Match keys outside SPEC-0 §5.3 — kept so strict validation still sees them. */
  extra: Record<string, unknown>;
  /** Whether the source had a `match:` key at all. */
  present: boolean;
}

export interface HeaderEditsDraft {
  add: Pair[];
  set: Pair[];
  remove: string[];
  present: boolean;
}

export interface RuleDraft {
  name: string;
  enabled: boolean;
  enabledPresent: boolean;
  action: RuleAction;
  match: MatchDraft;
  block: { mode: string; stub: string };
  mapLocal: { file: string; content_type: string; status: string };
  redirect: { scheme: string; host: string; port: string; path: string; query: string };
  headers: { request: HeaderEditsDraft; response: HeaderEditsDraft };
  body: { transform: string };
  /** Unmodelled top-level rule keys, preserved verbatim. */
  extra: Record<string, unknown>;
}

const MATCH_KEYS = [
  'host',
  'path',
  'method',
  'dest',
  'status',
  'content_type',
  'query',
  'request_headers',
];

/** Rule keys the form owns per action; everything else falls through to `extra`. */
const ACTION_KEYS: Record<RuleAction, string[]> = {
  passthrough: [],
  block: ['mode', 'stub'],
  map_local: ['file', 'content_type', 'status'],
  redirect: ['to'],
  headers: ['request', 'response'],
  body: ['transform'],
};

function emptyMulti(): MultiField {
  return { text: '', list: false };
}

function emptyHeaderEdits(): HeaderEditsDraft {
  return { add: [], set: [], remove: [], present: false };
}

export function emptyMatch(): MatchDraft {
  return {
    host: '',
    path: '',
    method: emptyMulti(),
    dest: emptyMulti(),
    status: emptyMulti(),
    content_type: '',
    query: [],
    request_headers: [],
    extra: {},
    present: false,
  };
}

export function emptyDraft(): RuleDraft {
  return {
    name: '',
    enabled: true,
    enabledPresent: false,
    action: 'block',
    match: emptyMatch(),
    block: { mode: '', stub: '' },
    mapLocal: { file: '', content_type: '', status: '' },
    redirect: { scheme: '', host: '', port: '', path: '', query: '' },
    headers: { request: emptyHeaderEdits(), response: emptyHeaderEdits() },
    body: { transform: '' },
    extra: {},
  };
}

/* ---------------------------- parsing in ---------------------------- */

function readMulti(value: unknown): MultiField {
  if (value === undefined || value === null) return emptyMulti();
  if (Array.isArray(value)) return { text: value.map(String).join(', '), list: true };
  return { text: String(value), list: false };
}

function readPairs(value: unknown): Pair[] {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return [];
  return Object.entries(value as Record<string, unknown>).map(([key, raw]) => ({
    key,
    value: raw === null || raw === undefined ? '' : String(raw),
    presenceOnly: raw === null || raw === undefined,
  }));
}

function readHeaderEdits(value: unknown): HeaderEditsDraft {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    return emptyHeaderEdits();
  }
  const record = value as Record<string, unknown>;
  const remove = Array.isArray(record['remove']) ? (record['remove'] as unknown[]).map(String) : [];
  return { add: readPairs(record['add']), set: readPairs(record['set']), remove, present: true };
}

function omit(source: Record<string, unknown>, keys: string[]): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(source)) {
    if (!keys.includes(key)) {
      // Keys come from a parsed YAML document under the user's own control,
      // and `out` is a fresh object literal, so there is no prototype-pollution
      // path here.
      // eslint-disable-next-line security/detect-object-injection
      out[key] = value;
    }
  }
  return out;
}

function asString(value: unknown): string {
  return value === undefined || value === null ? '' : String(value);
}

export function ruleToDraft(rule: Rule): RuleDraft {
  const draft = emptyDraft();
  const raw = rule as Record<string, unknown>;
  draft.name = asString(raw['name']);
  draft.action = (RULE_ACTIONS as string[]).includes(asString(raw['action']))
    ? (raw['action'] as RuleAction)
    : 'block';
  draft.enabledPresent = raw['enabled'] !== undefined;
  draft.enabled = raw['enabled'] === undefined ? true : Boolean(raw['enabled']);

  const match = raw['match'];
  if (match !== undefined && match !== null && typeof match === 'object') {
    const m = match as Record<string, unknown>;
    draft.match = {
      host: asString(m['host']),
      path: asString(m['path']),
      method: readMulti(m['method']),
      dest: readMulti(m['dest']),
      status: readMulti(m['status']),
      content_type: asString(m['content_type']),
      query: readPairs(m['query']),
      request_headers: readPairs(m['request_headers']),
      extra: omit(m, MATCH_KEYS),
      present: true,
    };
  }

  switch (draft.action) {
    case 'block':
      draft.block = {
        mode: asString(raw['mode']),
        stub: typeof raw['stub'] === 'string' ? raw['stub'] : '',
      };
      break;
    case 'map_local':
      draft.mapLocal = {
        file: asString(raw['file']),
        content_type: asString(raw['content_type']),
        status: asString(raw['status']),
      };
      break;
    case 'redirect': {
      const to = raw['to'];
      if (to !== null && typeof to === 'object' && !Array.isArray(to)) {
        const t = to as Record<string, unknown>;
        draft.redirect = {
          scheme: asString(t['scheme']),
          host: asString(t['host']),
          port: asString(t['port']),
          path: asString(t['path']),
          query: asString(t['query']),
        };
      }
      break;
    }
    case 'headers':
      draft.headers = {
        request: readHeaderEdits(raw['request']),
        response: readHeaderEdits(raw['response']),
      };
      break;
    case 'body':
      draft.body = { transform: typeof raw['transform'] === 'string' ? raw['transform'] : '' };
      break;
    default:
      break;
  }

  // An inline stub spec, a `transforms` list, a RedirectSpec with extra keys:
  // none of these have a form control, and all of them survive here.
  const owned = ['name', 'enabled', 'match', 'action', ...ACTION_KEYS[draft.action]];
  const extra = omit(raw, owned);
  if (draft.action === 'block' && raw['stub'] !== undefined && typeof raw['stub'] !== 'string') {
    extra['stub'] = raw['stub'];
  }
  if (draft.action === 'body' && raw['transform'] !== undefined && draft.body.transform === '') {
    extra['transform'] = raw['transform'];
  }
  draft.extra = extra;
  return draft;
}

/* ---------------------------- emitting out ---------------------------- */

function splitList(text: string): string[] {
  return text
    .split(',')
    .map((part) => part.trim())
    .filter((part) => part.length > 0);
}

function writeMulti(field: MultiField): unknown {
  const parts = splitList(field.text);
  if (parts.length === 0) return undefined;
  if (parts.length === 1 && !field.list) return parts[0];
  return parts;
}

/** Status accepts integers and `"300-399"` range strings; keep integers integral. */
function writeStatus(field: MultiField): unknown {
  const parts = splitList(field.text).map((part) => (/^\d+$/.test(part) ? Number(part) : part));
  if (parts.length === 0) return undefined;
  if (parts.length === 1 && !field.list) return parts[0];
  return parts;
}

function writePairs(pairs: Pair[], allowNull: boolean): Record<string, unknown> | undefined {
  const usable = pairs.filter((pair) => pair.key.trim() !== '');
  if (usable.length === 0) return undefined;
  const out: Record<string, unknown> = {};
  for (const pair of usable) {
    // Object literal target, keys from the user's own module file.
    out[pair.key] = allowNull && pair.presenceOnly ? null : pair.value;
  }
  return out;
}

function writeHeaderEdits(draft: HeaderEditsDraft): Record<string, unknown> | undefined {
  const out: Record<string, unknown> = {};
  const add = writePairs(draft.add, false);
  const set = writePairs(draft.set, false);
  const remove = draft.remove.filter((name) => name.trim() !== '');
  if (add) out['add'] = add;
  if (remove.length > 0) out['remove'] = remove;
  if (set) out['set'] = set;
  if (Object.keys(out).length === 0) return draft.present ? {} : undefined;
  return out;
}

function assign(target: Record<string, unknown>, key: string, value: unknown): void {
  if (value === undefined) return;
  // eslint-disable-next-line security/detect-object-injection
  target[key] = value;
}

export function draftToMatch(draft: MatchDraft): Match | undefined {
  const out: Record<string, unknown> = {};
  if (draft.host.trim() !== '') out['host'] = draft.host;
  if (draft.path.trim() !== '') out['path'] = draft.path;
  assign(out, 'method', writeMulti(draft.method));
  assign(out, 'dest', writeMulti(draft.dest));
  assign(out, 'status', writeStatus(draft.status));
  if (draft.content_type.trim() !== '') out['content_type'] = draft.content_type;
  assign(out, 'query', writePairs(draft.query, false));
  assign(out, 'request_headers', writePairs(draft.request_headers, true));
  for (const [key, value] of Object.entries(draft.extra)) assign(out, key, value);
  if (Object.keys(out).length === 0) return draft.present ? ({} as Match) : undefined;
  return out as Match;
}

export function draftToRule(draft: RuleDraft): Rule {
  const out: Record<string, unknown> = { name: draft.name };
  if (draft.enabledPresent || draft.enabled === false) out['enabled'] = draft.enabled;
  assign(out, 'match', draftToMatch(draft.match));
  out['action'] = draft.action;

  switch (draft.action) {
    case 'block':
      if (draft.block.mode.trim() !== '') out['mode'] = draft.block.mode;
      if (draft.block.stub.trim() !== '') out['stub'] = draft.block.stub;
      break;
    case 'map_local':
      if (draft.mapLocal.file.trim() !== '') out['file'] = draft.mapLocal.file;
      if (draft.mapLocal.content_type.trim() !== '')
        out['content_type'] = draft.mapLocal.content_type;
      if (draft.mapLocal.status.trim() !== '') {
        const status = draft.mapLocal.status.trim();
        out['status'] = /^\d+$/.test(status) ? Number(status) : status;
      }
      break;
    case 'redirect': {
      const to: Record<string, unknown> = {};
      const { scheme, host, port, path, query } = draft.redirect;
      if (scheme.trim() !== '') to['scheme'] = scheme;
      if (host.trim() !== '') to['host'] = host;
      if (port.trim() !== '') to['port'] = /^\d+$/.test(port.trim()) ? Number(port.trim()) : port;
      if (path.trim() !== '') to['path'] = path;
      if (query.trim() !== '') to['query'] = query;
      if (Object.keys(to).length > 0) out['to'] = to;
      break;
    }
    case 'headers':
      assign(out, 'request', writeHeaderEdits(draft.headers.request));
      assign(out, 'response', writeHeaderEdits(draft.headers.response));
      break;
    case 'body':
      if (draft.body.transform.trim() !== '') out['transform'] = draft.body.transform;
      break;
    default:
      break;
  }

  for (const [key, value] of Object.entries(draft.extra)) assign(out, key, value);
  return out as Rule;
}

/** The builder's live preview (SPEC-2 §7.3). */
export function previewYaml(draft: RuleDraft): string {
  return stringifyRule(draftToRule(draft));
}

/** Parse the free-form "other keys" editor; returns null when it does not parse. */
export function parseExtraYaml(text: string): Record<string, unknown> | null {
  if (text.trim() === '') return {};
  const doc = parseDocument(text, { prettyErrors: false });
  if (doc.errors.length > 0) return null;
  const value = doc.toJS() as unknown;
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return null;
  return value as Record<string, unknown>;
}
