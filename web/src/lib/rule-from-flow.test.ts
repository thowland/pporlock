import { describe, expect, it } from 'vitest';
import { INTENT_LABELS, escapeRegex, ruleFromFlow, slugify } from './rule-from-flow';
import { makeFlow } from '../test/factories';

describe('escapeRegex', () => {
  // A suggested rule is built from attacker-influenced URL text. An unescaped
  // metacharacter would silently widen what the rule matches.
  it('neutralises every regex metacharacter', () => {
    expect(escapeRegex('/a.*+?^${}()|[]\\b')).toBe(
      '/a\\.\\*\\+\\?\\^\\$\\{\\}\\(\\)\\|\\[\\]\\\\b',
    );
  });
});

describe('slugify', () => {
  it('produces a module-name slug', () => {
    expect(slugify('CDN.example.com')).toBe('cdn-example-com');
    expect(slugify('!!!')).toBe('rule');
  });
});

describe('ruleFromFlow  # REQ WUI-008', () => {
  it('populates match from the flow method, host, path and dest', () => {
    const rule = ruleFromFlow(makeFlow(), 'block');
    expect(rule).toMatchObject({
      name: 'block-cdn-example-com',
      action: 'block',
      mode: 'stub',
      match: {
        host: 'cdn.example.com',
        path: '^/a/analytics\\.js$',
        method: 'GET',
        dest: 'script',
      },
    });
  });

  it('offers an inert default for each intent', () => {
    expect(ruleFromFlow(makeFlow(), 'map_local')).toMatchObject({ file: '' });
    expect(ruleFromFlow(makeFlow(), 'redirect')).toMatchObject({
      to: { host: 'cdn.example.com' },
    });
    expect(ruleFromFlow(makeFlow(), 'headers')).toMatchObject({ request: { set: {} } });
    expect(ruleFromFlow(makeFlow(), 'map_local').name).toBe('map-local-cdn-example-com');
  });

  it('drops the query string from the suggested path', () => {
    const flow = makeFlow({
      request: { ...makeFlow().request!, path: '/collect?tid=UA-1' },
    });
    expect((ruleFromFlow(flow, 'block')['match'] as Record<string, unknown>)['path']).toBe(
      '^/collect$',
    );
  });

  it('falls back to the passthrough host when there is no request', () => {
    const flow = makeFlow({
      kind: 'passthrough',
      request: undefined,
      response: undefined,
      passthrough: { host: 'excluded.example' },
    });
    const rule = ruleFromFlow(flow, 'block');
    expect(rule['match']).toEqual({ host: 'excluded.example' });
    expect(rule.name).toBe('block-excluded-example');
  });

  it('names all four intents for the picker', () => {
    expect(Object.keys(INTENT_LABELS)).toEqual(['block', 'map_local', 'redirect', 'headers']);
  });
});
