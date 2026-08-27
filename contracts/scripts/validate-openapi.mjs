#!/usr/bin/env node
// Validates contracts/openapi.yaml parses, is structurally sane, and that every
// external $ref points at a schema file that exists (REQ API-029).
//
// This is not a full OpenAPI linter. It checks the things that actually break:
// a dangling schema reference, a path with no operations, an operation with no
// responses — each of which produces a contract that lies about the server.
import { readFileSync, existsSync } from 'node:fs';
import { join, dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parse } from 'yaml';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const specPath = join(root, 'openapi.yaml');

let spec;
try {
  spec = parse(readFileSync(specPath, 'utf8'));
} catch (e) {
  console.error(`FAIL openapi.yaml: ${e.message}`);
  process.exit(1);
}

const problems = [];
const METHODS = ['get', 'post', 'put', 'patch', 'delete', 'head', 'options'];

if (!spec.openapi?.startsWith('3.')) problems.push('openapi version must be 3.x');
if (!spec.info?.title || !spec.info?.version) problems.push('info.title and info.version required');
if (!spec.paths || Object.keys(spec.paths).length === 0) problems.push('no paths defined');

// Every external $ref must resolve to a file on disk.
const refs = new Set();
(function walk(node) {
  if (node === null || typeof node !== 'object') return;
  if (Array.isArray(node)) return node.forEach(walk);
  for (const [k, v] of Object.entries(node)) {
    if (k === '$ref' && typeof v === 'string' && !v.startsWith('#')) refs.add(v);
    else walk(v);
  }
})(spec);

for (const ref of refs) {
  const [file] = ref.split('#');
  if (!existsSync(resolve(root, file))) problems.push(`dangling $ref: ${ref}`);
}

// Every path must have at least one operation, and every operation responses.
let operations = 0;
for (const [path, item] of Object.entries(spec.paths)) {
  const ops = METHODS.filter((m) => item[m]);
  if (ops.length === 0) problems.push(`${path}: no operations`);
  for (const m of ops) {
    operations++;
    const op = item[m];
    if (!op.responses || Object.keys(op.responses).length === 0) {
      problems.push(`${m.toUpperCase()} ${path}: no responses declared`);
    }
    if (!op.summary && !op.description) {
      problems.push(`${m.toUpperCase()} ${path}: undocumented`);
    }
  }
}

if (problems.length > 0) {
  for (const p of problems) console.error(`FAIL ${p}`);
  console.error(`\n${problems.length} problem(s) in openapi.yaml`);
  process.exit(1);
}

console.log(
  `ok   openapi.yaml — ${Object.keys(spec.paths).length} paths, ${operations} operations, ` +
    `${refs.size} external refs resolved`,
);
