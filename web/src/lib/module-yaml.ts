/**
 * `module.yaml` reading and *surgical* writing (SPEC-2 §7.3, REQ WUI-007).
 *
 * The rule builder is a generator for the canonical format, not a parallel
 * representation of it. That imposes a hard constraint the spec states
 * outright: round-tripping a rule through the builder must not reformat or
 * reorder unrelated rules.
 *
 * A naive implementation parses to a plain object and re-serialises the whole
 * document. That loses comments, normalises quoting, and rewrites every rule
 * the user did not touch — the diff of a one-word edit becomes the whole file.
 * So instead we parse for structure, take the *source range* of the single rule
 * being replaced, and splice text. Every byte outside that range survives
 * untouched, comments included.
 */
import { LineCounter, Document, isSeq, parseDocument } from 'yaml';
import type { ModuleManifest, Rule } from '../api/types';

export interface YamlIssue {
  line: number;
  column: number;
  message: string;
}

/** A no-op edit is not an edit: deep equality decides whether we write at all. */
export function deepEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (typeof a !== typeof b) return false;
  if (a === null || b === null || typeof a !== 'object') return false;
  if (Array.isArray(a) !== Array.isArray(b)) return false;
  if (Array.isArray(a) && Array.isArray(b)) {
    // Numeric array indices, not property names.
    // eslint-disable-next-line security/detect-object-injection
    return a.length === b.length && a.every((item, i) => deepEqual(item, b[i]));
  }
  const left = a as Record<string, unknown>;
  const right = b as Record<string, unknown>;
  const leftKeys = Object.keys(left).sort();
  const rightKeys = Object.keys(right).sort();
  if (leftKeys.length !== rightKeys.length) return false;
  // eslint-disable-next-line security/detect-object-injection
  if (!leftKeys.every((k, i) => k === rightKeys[i])) return false;
  // Keys come from Object.keys of a parsed document, never from a URL or a
  // header, so there is no attacker-chosen property name in play here.
  /* eslint-disable security/detect-object-injection */
  return leftKeys.every((k) => deepEqual(left[k], right[k]));
  /* eslint-enable security/detect-object-injection */
}

/**
 * YAML syntax errors as editor markers. The daemon is the authority on schema
 * validity (`POST /validate`), but it cannot answer while you are mid-keystroke
 * — this covers the "you left a bracket open" case locally and instantly.
 */
export function yamlIssues(text: string): YamlIssue[] {
  const counter = new LineCounter();
  const doc = parseDocument(text, { lineCounter: counter, prettyErrors: false });
  return doc.errors.map((error) => {
    const pos = counter.linePos(error.pos[0]);
    return { line: pos.line, column: pos.col, message: error.message };
  });
}

/** The manifest as a plain object, or null when the text does not parse. */
export function readManifest(text: string): ModuleManifest | null {
  const doc = parseDocument(text, { prettyErrors: false });
  if (doc.errors.length > 0) return null;
  const value = doc.toJS() as unknown;
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return null;
  return value as ModuleManifest;
}

export function readRules(text: string): Rule[] {
  const manifest = readManifest(text);
  const rules = manifest?.rules;
  return Array.isArray(rules) ? rules : [];
}

/** Serialise any plain value to YAML with the formatting this project emits. */
export function stringifyYaml(value: unknown): string {
  return new Document(value).toString({ lineWidth: 0, indent: 2 });
}

/** Serialise one rule on its own, for the builder's live YAML preview. */
export function stringifyRule(rule: Rule): string {
  return stringifyYaml(rule);
}

function indentBlock(block: string, spaces: number): string {
  const pad = ' '.repeat(spaces);
  const lines = block.replace(/\n$/, '').split('\n');
  return lines.map((line, i) => (i === 0 || line === '' ? line : pad + line)).join('\n') + '\n';
}

interface RuleSpan {
  start: number;
  end: number;
  indent: number;
  current: unknown;
}

function ruleSpan(text: string, index: number): RuleSpan | null {
  const counter = new LineCounter();
  const doc = parseDocument(text, { lineCounter: counter, prettyErrors: false });
  if (doc.errors.length > 0) return null;
  const seq = doc.get('rules');
  if (!isSeq(seq)) return null;
  const item = seq.items.at(index);
  if (item === undefined || typeof item !== 'object' || item === null) return null;
  const range = (item as { range?: [number, number, number] }).range;
  if (!range) return null;
  const toJSON = (item as { toJSON?: () => unknown }).toJSON;
  return {
    start: range[0],
    end: range[1],
    indent: counter.linePos(range[0]).col - 1,
    current: typeof toJSON === 'function' ? toJSON.call(item) : null,
  };
}

/**
 * Replace `rules[index]` with `rule`, touching nothing else in the file.
 *
 * Returns the text unchanged when the rule is semantically identical — an
 * opened-and-closed builder must not produce a diff.
 */
export function writeRule(text: string, index: number, rule: Rule): string {
  const span = ruleSpan(text, index);
  if (span === null) return text;
  if (deepEqual(span.current, rule)) return text;
  const block = indentBlock(stringifyRule(rule), span.indent);
  return text.slice(0, span.start) + block + text.slice(span.end);
}

/**
 * Append a rule, creating the `rules:` key if the manifest has none. Existing
 * content is preserved byte-for-byte; only the tail grows.
 */
export function appendRule(text: string, rule: Rule): string {
  const body = stringifyRule(rule);
  const existing = ruleSpanOfLast(text);
  if (existing === null) {
    const base = text.length === 0 || text.endsWith('\n') ? text : `${text}\n`;
    return `${base}rules:\n  - ${indentBlock(body, 4).trimEnd()}\n`;
  }
  const head = text.slice(0, existing.end).replace(/\s*$/, '\n');
  const tail = text.slice(existing.end).replace(/^\n+/, '');
  const dash = `${' '.repeat(Math.max(existing.indent - 2, 0))}- `;
  return `${head}${dash}${indentBlock(body, existing.indent).trimEnd()}\n${tail}`;
}

function ruleSpanOfLast(text: string): RuleSpan | null {
  const rules = readRules(text);
  if (rules.length === 0) return null;
  return ruleSpan(text, rules.length - 1);
}

/** Index of the rule with this name, or -1. Names are unique within a module. */
export function findRuleIndex(text: string, name: string): number {
  return readRules(text).findIndex((rule) => rule.name === name);
}
