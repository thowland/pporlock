#!/usr/bin/env node
// Generates contracts/generated/types.ts from contracts/schemas/.
// TypeScript wire types are GENERATED, never hand-written (SPEC-0 §1.1).
import { readdirSync, readFileSync, mkdirSync, writeFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { compile } from 'json-schema-to-typescript';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const schemaDir = join(root, 'schemas');
const outDir = join(root, 'generated');

const files = readdirSync(schemaDir).filter((f) => f.endsWith('.schema.json')).sort();
if (files.length === 0) {
  console.error('FAIL: no schemas found in contracts/schemas/');
  process.exit(1);
}

const banner = [
  '/* eslint-disable */',
  '// GENERATED FILE — DO NOT EDIT.',
  '// Source: contracts/schemas/. Regenerate with `make contracts`.',
  '// Hand-writing a type that describes a wire shape is a structural violation',
  '// of SPEC-0 §1.1; add it to the schema instead.',
  '',
].join('\n');

const parts = [banner];
for (const file of files) {
  const schema = JSON.parse(readFileSync(join(schemaDir, file), 'utf8'));
  const ts = await compile(schema, schema.title ?? file, {
    bannerComment: '',
    additionalProperties: false,
    style: { singleQuote: true, printWidth: 100 },
  });
  parts.push(ts.trimEnd(), '');
  console.log(`generated ${file}`);
}

mkdirSync(outDir, { recursive: true });
writeFileSync(join(outDir, 'types.ts'), parts.join('\n'));
console.log(`\nwrote ${join('contracts', 'generated', 'types.ts')}`);
