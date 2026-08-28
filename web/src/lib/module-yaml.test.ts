import { describe, expect, it } from 'vitest';
import {
  appendRule,
  deepEqual,
  findRuleIndex,
  readManifest,
  readRules,
  stringifyRule,
  stringifyYaml,
  writeRule,
  yamlIssues,
} from './module-yaml';
import { draftToRule, ruleToDraft } from './rule-draft';
import { FIXTURE_MODULE_YAML } from '../test/module-fixture';
import type { Rule } from '../api/types';

describe('reading module.yaml', () => {
  it('reads the manifest and its rules', () => {
    const manifest = readManifest(FIXTURE_MODULE_YAML);
    expect(manifest?.name).toBe('block-vendors');
    expect(manifest?.pporlock_api).toBe('1');
    expect(readRules(FIXTURE_MODULE_YAML).map((rule) => rule.name)).toEqual([
      'block-analytics-vendor',
      'block-with-inline-stub',
      'strip-csp-on-html',
      'add-debug-header',
      'send-to-fixture',
    ]);
  });

  it('returns null for text that is not a mapping', () => {
    expect(readManifest('- a\n- b\n')).toBeNull();
    expect(readManifest('name: [oops\n')).toBeNull();
    expect(readRules('name: [oops\n')).toEqual([]);
  });

  it('reports YAML syntax errors with a line and column for the gutter', () => {
    const issues = yamlIssues('name: ok\nrules:\n  - name: [unclosed\n');
    expect(issues.length).toBeGreaterThan(0);
    expect(issues[0]!.line).toBeGreaterThan(0);
    expect(issues[0]!.column).toBeGreaterThan(0);
  });

  it('reports nothing for valid YAML', () => {
    expect(yamlIssues(FIXTURE_MODULE_YAML)).toEqual([]);
  });

  it('finds a rule by name', () => {
    expect(findRuleIndex(FIXTURE_MODULE_YAML, 'strip-csp-on-html')).toBe(2);
    expect(findRuleIndex(FIXTURE_MODULE_YAML, 'nope')).toBe(-1);
  });
});

/**
 * SPEC-2 §7.3 states the constraint plainly: round-tripping through the builder
 * must not reformat or reorder unrelated rules. These are the tests that hold
 * the implementation to it.
 */
describe('round-trip stability  # REQ WUI-007', () => {
  it('leaves the file byte-identical when every rule is written back unchanged', () => {
    const rules = readRules(FIXTURE_MODULE_YAML);
    let text = FIXTURE_MODULE_YAML;
    rules.forEach((rule, index) => {
      // Exactly what the builder does: parse into the form model, emit, write.
      text = writeRule(text, index, draftToRule(ruleToDraft(rule)));
    });
    expect(text).toBe(FIXTURE_MODULE_YAML);
  });

  it('rewrites only the edited rule and leaves every other line untouched', () => {
    const rules = readRules(FIXTURE_MODULE_YAML);
    const draft = ruleToDraft(rules[2]!);
    draft.body.transform = 'strip_integrity_attributes';
    const next = writeRule(FIXTURE_MODULE_YAML, 2, draftToRule(draft));

    expect(next).not.toBe(FIXTURE_MODULE_YAML);
    expect(readRules(next)[2]).toMatchObject({ transform: 'strip_integrity_attributes' });

    // Every other rule survives, values and all.
    const before = readRules(FIXTURE_MODULE_YAML);
    const after = readRules(next);
    expect(after.length).toBe(before.length);
    [0, 1, 3, 4].forEach((index) => expect(after[index]).toEqual(before[index]));

    // And so does everything outside the rules list, comments included.
    expect(next).toContain('# First match wins for short-circuit actions (REQ MOD-012).');
    expect(next).toContain(
      '  # An inline stub spec: the builder has no form for this and must not eat it.',
    );
    expect(next).toContain('config:\n  vendor_list: []\n');
    expect(next.startsWith('name: block-vendors\nversion: 1.2.0\npporlock_api: "1"\n')).toBe(true);
  });

  it('preserves the comment that follows the edited rule', () => {
    const rules = readRules(FIXTURE_MODULE_YAML);
    const draft = ruleToDraft(rules[0]!);
    draft.match.host = 'other.example';
    const next = writeRule(FIXTURE_MODULE_YAML, 0, draftToRule(draft));
    expect(next).toContain(
      '  # An inline stub spec: the builder has no form for this and must not eat it.',
    );
    expect(readRules(next)[1]).toEqual(rules[1]);
  });

  it('is a no-op when the index does not exist or the file does not parse', () => {
    const rule: Rule = { name: 'x', action: 'block' };
    expect(writeRule(FIXTURE_MODULE_YAML, 99, rule)).toBe(FIXTURE_MODULE_YAML);
    expect(writeRule('name: [oops\n', 0, rule)).toBe('name: [oops\n');
    expect(writeRule('name: no-rules\n', 0, rule)).toBe('name: no-rules\n');
  });
});

describe('appendRule', () => {
  const rule: Rule = { name: 'new-rule', match: { host: 'x.example' }, action: 'block' };

  it('adds to an existing rules list without disturbing it', () => {
    const next = appendRule(FIXTURE_MODULE_YAML, rule);
    const after = readRules(next);
    expect(after).toHaveLength(6);
    expect(after[5]).toEqual(rule);
    expect(readRules(FIXTURE_MODULE_YAML)).toEqual(after.slice(0, 5));
    expect(next).toContain('# First match wins for short-circuit actions (REQ MOD-012).');
  });

  it('creates the rules key when the manifest has none', () => {
    const next = appendRule('name: bare\nversion: 0.1.0\npporlock_api: "1"\n', rule);
    expect(readRules(next)).toEqual([rule]);
  });

  it('tolerates a manifest that does not end in a newline', () => {
    const next = appendRule('name: bare', rule);
    expect(readRules(next)).toEqual([rule]);
  });

  it('keeps trailing document content after the rules list', () => {
    const next = appendRule(FIXTURE_MODULE_YAML, rule);
    expect(readManifest(next)?.config).toEqual({ vendor_list: [] });
  });
});

describe('stringify helpers', () => {
  it('emits block style with two-space indent and no wrapping', () => {
    expect(stringifyRule({ name: 'a', action: 'block' })).toBe('name: a\naction: block\n');
    expect(stringifyYaml({ a: [1, 2] })).toBe('a:\n  - 1\n  - 2\n');
  });
});

describe('deepEqual', () => {
  it('compares structurally, ignoring key order', () => {
    expect(deepEqual({ a: 1, b: [1, { c: 2 }] }, { b: [1, { c: 2 }], a: 1 })).toBe(true);
  });

  it('separates arrays from objects and differing shapes', () => {
    expect(deepEqual([1], { 0: 1 })).toBe(false);
    expect(deepEqual({ a: 1 }, { a: 1, b: 2 })).toBe(false);
    expect(deepEqual({ a: 1 }, { b: 1 })).toBe(false);
    expect(deepEqual([1, 2], [1, 3])).toBe(false);
    expect(deepEqual(1, '1')).toBe(false);
    expect(deepEqual(null, {})).toBe(false);
  });
});
