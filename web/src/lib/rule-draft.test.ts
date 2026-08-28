import { describe, expect, it } from 'vitest';
import { readRules, stringifyRule } from './module-yaml';
import {
  draftToRule,
  emptyDraft,
  parseExtraYaml,
  previewYaml,
  ruleToDraft,
  type RuleDraft,
} from './rule-draft';
import { FIXTURE_MODULE_YAML } from '../test/module-fixture';

const FIXTURE_RULES = readRules(FIXTURE_MODULE_YAML);

describe('rule builder round trip', () => {
  it('has a fixture covering every action the builder offers', () => {
    expect(FIXTURE_RULES).toHaveLength(5);
    expect(new Set(FIXTURE_RULES.map((rule) => rule.action))).toEqual(
      new Set(['block', 'body', 'headers', 'redirect']),
    );
  });

  // REQ WUI-007: the builder is a generator for the canonical format, so a
  // rule that goes in must come back out semantically identical.
  it.each(FIXTURE_RULES.map((rule) => [rule.name, rule] as const))(
    'round-trips %s through the draft model unchanged  # REQ WUI-007',
    (_name, rule) => {
      expect(draftToRule(ruleToDraft(rule))).toEqual(rule);
    },
  );

  it('keeps a one-element list a list rather than collapsing it  # REQ WUI-007', () => {
    const rule = FIXTURE_RULES.find((r) => r.name === 'block-with-inline-stub');
    expect(rule).toBeDefined();
    const draft = ruleToDraft(rule!);
    expect(draft.match.method).toEqual({ text: 'POST', list: true });
    expect(draftToRule(draft)['match']).toMatchObject({ method: ['POST'] });
  });

  it('keeps an inline stub spec the form cannot render  # REQ WUI-007', () => {
    const rule = FIXTURE_RULES.find((r) => r.name === 'block-with-inline-stub')!;
    const draft = ruleToDraft(rule);
    expect(draft.block.stub).toBe('');
    expect(draft.extra['stub']).toEqual({
      status: 200,
      content_type: 'application/javascript',
      body: 'window.analytics={track(){}};',
    });
  });

  it('preserves an explicit enabled: false and omits an absent one', () => {
    const disabled = FIXTURE_RULES.find((r) => r.name === 'strip-csp-on-html')!;
    expect(draftToRule(ruleToDraft(disabled))['enabled']).toBe(false);

    const plain = FIXTURE_RULES.find((r) => r.name === 'add-debug-header')!;
    expect('enabled' in draftToRule(ruleToDraft(plain))).toBe(false);
  });

  it('preserves a presence-only header criterion as null', () => {
    const rule = FIXTURE_RULES.find((r) => r.name === 'block-analytics-vendor')!;
    const draft = ruleToDraft(rule);
    const presence = draft.match.request_headers.find((pair) => pair.key === 'x-requested-with');
    expect(presence?.presenceOnly).toBe(true);
    const emitted = draftToRule(draft)['match'] as Record<string, unknown>;
    expect((emitted['request_headers'] as Record<string, unknown>)['x-requested-with']).toBeNull();
  });

  it('keeps integer statuses integral and range strings stringy', () => {
    const rule = FIXTURE_RULES.find((r) => r.name === 'strip-csp-on-html')!;
    const emitted = draftToRule(ruleToDraft(rule))['match'] as Record<string, unknown>;
    expect(emitted['status']).toEqual([200, '300-399']);
  });

  it('emits a redirect port as a number', () => {
    const rule = FIXTURE_RULES.find((r) => r.name === 'send-to-fixture')!;
    const emitted = draftToRule(ruleToDraft(rule)) as Record<string, unknown>;
    expect(emitted['to']).toEqual({
      scheme: 'http',
      host: '127.0.0.1',
      port: 8099,
      path: '/stub.js',
    });
  });
});

describe('draft editing', () => {
  it('drops a match criterion when its field is cleared', () => {
    const draft = ruleToDraft(FIXTURE_RULES[3]!);
    const cleared: RuleDraft = { ...draft, match: { ...draft.match, host: '  ' } };
    expect((draftToRule(cleared)['match'] as Record<string, unknown>)['host']).toBeUndefined();
  });

  it('omits match entirely when the source had none', () => {
    const draft = emptyDraft();
    draft.name = 'no-match';
    expect('match' in draftToRule(draft)).toBe(false);
  });

  it('ignores pair rows with a blank key', () => {
    const draft = emptyDraft();
    draft.name = 'q';
    draft.match.query = [
      { key: '', value: 'ignored' },
      { key: 'tid', value: '^UA-' },
    ];
    expect((draftToRule(draft)['match'] as Record<string, unknown>)['query']).toEqual({
      tid: '^UA-',
    });
  });

  it('previews the same YAML the module file will receive', () => {
    const draft = ruleToDraft(FIXTURE_RULES[0]!);
    expect(previewYaml(draft)).toBe(stringifyRule(draftToRule(draft)));
  });
});

describe('parseExtraYaml', () => {
  it('treats empty text as no extra keys', () => {
    expect(parseExtraYaml('   ')).toEqual({});
  });

  it('rejects a sequence and unparseable text rather than guessing', () => {
    expect(parseExtraYaml('- a\n- b\n')).toBeNull();
    expect(parseExtraYaml('a: [1,\nb: 2\n')).toBeNull();
  });

  it('accepts a mapping', () => {
    expect(parseExtraYaml('transforms:\n  - strip_csp\n')).toEqual({ transforms: ['strip_csp'] });
  });
});
