/**
 * Deriving a candidate rule from an observed flow (SPEC-2 §7.4, REQ WUI-008).
 *
 * The daemon owns this derivation (`POST /flows/{id}/suggest-rule`) so the UI,
 * the DevTools panel, and MCP all propose the same rule. This module is the
 * local fallback for when that call fails — an offline or older daemon should
 * degrade to a slightly less clever suggestion, not to a dead button.
 */
import type { FlowRecord, Rule, RuleIntent } from '../api/types';

/**
 * A path becomes a regex, and a path is attacker-influenced content. Escaping
 * every metacharacter means a URL containing `.*` matches that literal text and
 * nothing else — the alternative is a suggested rule that blocks far more than
 * the user pointed at.
 */
export function escapeRegex(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

export function slugify(value: string): string {
  const slug = value
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '');
  return slug === '' ? 'rule' : slug;
}

export const INTENT_LABELS: Record<RuleIntent, string> = {
  block: 'Block',
  map_local: 'Map local',
  redirect: 'Redirect',
  headers: 'Edit headers',
};

export function ruleFromFlow(flow: FlowRecord, intent: RuleIntent): Rule {
  const request = flow.request;
  const host = request?.host ?? flow.passthrough?.host ?? '';
  const path = request?.path ?? '';
  const match: Record<string, unknown> = {};
  if (host !== '') match['host'] = host;
  if (path !== '') match['path'] = `^${escapeRegex(path.split('?')[0] ?? path)}$`;
  if (request?.method) match['method'] = request.method;
  if (request?.dest) match['dest'] = request.dest;

  const rule: Record<string, unknown> = {
    name: `${intent.replace('_', '-')}-${slugify(host || 'flow')}`,
    match,
    action: intent,
  };

  // Sensible, inert defaults: a suggestion should be reviewable, and none of
  // these do anything surprising before the user fills them in.
  if (intent === 'block') rule['mode'] = 'stub';
  if (intent === 'map_local') rule['file'] = '';
  if (intent === 'redirect') rule['to'] = { host: host };
  if (intent === 'headers') rule['request'] = { set: {} };

  return rule as Rule;
}
