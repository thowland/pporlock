#!/usr/bin/env node
// Generates contracts/generated/types.ts from contracts/schemas/.
// TypeScript wire types are GENERATED, never hand-written (SPEC-0 §1.1).
//
// Schemas cross-reference each other by $id. json-schema-to-typescript resolves
// those through a resolver that maps $id back to the local file, so no schema
// needs to know where another one lives on disk.
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

const byId = new Map();
for (const file of files) {
  const schema = JSON.parse(readFileSync(join(schemaDir, file), 'utf8'));
  byId.set(schema.$id, { file, schema });
}

const idResolver = {
  order: 1,
  canRead: (f) => byId.has(f.url) || byId.has(f.url.replace(/^file:\/\//, '')),
  read: (f) => {
    const entry = byId.get(f.url) ?? byId.get(f.url.replace(/^file:\/\//, ''));
    if (!entry) throw new Error(`unresolvable $ref: ${f.url}`);
    return JSON.stringify(entry.schema);
  },
};

const banner = [
  '/* eslint-disable */',
  '// GENERATED FILE — DO NOT EDIT.',
  '// Source: contracts/schemas/. Regenerate with `make contracts`.',
  '// Hand-writing a type that describes a wire shape is a structural violation',
  '// of SPEC-0 §1.1; add it to the schema instead.',
  '',
].join('\n');

const parts = [banner];
const seen = new Set();
for (const file of files) {
  const { schema } = byId.get(
    [...byId.entries()].find(([, v]) => v.file === file)[0],
  );
  const ts = await compile(schema, schema.title ?? file, {
    bannerComment: '',
    additionalProperties: false,
    declareExternallyReferenced: true,
    $refOptions: { resolve: { pporlockId: idResolver } },
    style: { singleQuote: true, printWidth: 100 },
  });
  // Cross-referenced schemas get emitted into more than one file's output;
  // keep the first definition of each interface and drop the duplicates.
  for (const block of ts.split(/\n(?=export (?:interface|type) )/)) {
    const name = block.match(/export (?:interface|type) (\w+)/)?.[1];
    if (!name) {
      if (block.trim()) parts.push(block.trimEnd(), '');
      continue;
    }
    if (seen.has(name)) continue;
    seen.add(name);
    parts.push(block.trimEnd(), '');
  }
  console.log(`generated ${file}`);
}

mkdirSync(outDir, { recursive: true });
writeFileSync(join(outDir, 'types.ts'), parts.join('\n'));
console.log(`\nwrote ${join('contracts', 'generated', 'types.ts')}`);
