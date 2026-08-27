#!/usr/bin/env node
// Validates every JSON Schema in contracts/schemas/ compiles under draft 2020-12,
// and that each declares $id and $schema. SPEC-0 §1.1 / REQ MOD-015.
//
// Schemas cross-reference each other by $id (flow -> provenance), so every
// schema is registered with the validator before any is compiled.
import { readdirSync, readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import Ajv2020 from 'ajv/dist/2020.js';
import addFormats from 'ajv-formats';

const schemaDir = join(dirname(fileURLToPath(import.meta.url)), '..', 'schemas');
const ajv = new Ajv2020({ strict: true, allErrors: true });
addFormats(ajv);

const files = readdirSync(schemaDir).filter((f) => f.endsWith('.schema.json')).sort();
if (files.length === 0) {
  console.error('FAIL: no schemas found in contracts/schemas/');
  process.exit(1);
}

// --- pass 1: parse, check metadata, register ---
const parsed = [];
let failed = 0;
for (const file of files) {
  let schema;
  try {
    schema = JSON.parse(readFileSync(join(schemaDir, file), 'utf8'));
  } catch (e) {
    console.error(`FAIL ${file}: invalid JSON — ${e.message}`);
    failed++;
    continue;
  }
  if (!schema.$id) {
    console.error(`FAIL ${file}: missing $id`);
    failed++;
    continue;
  }
  if (schema.$schema !== 'https://json-schema.org/draft/2020-12/schema') {
    console.error(`FAIL ${file}: $schema must be draft 2020-12`);
    failed++;
    continue;
  }
  try {
    ajv.addSchema(schema, schema.$id);
    parsed.push({ file, schema });
  } catch (e) {
    console.error(`FAIL ${file}: ${e.message}`);
    failed++;
  }
}

// --- pass 2: compile, now that cross-references can resolve ---
for (const { file, schema } of parsed) {
  try {
    ajv.getSchema(schema.$id);
    ajv.compile(schema);
    console.log(`ok   ${file}`);
  } catch (e) {
    console.error(`FAIL ${file}: ${e.message}`);
    failed++;
  }
}

if (failed > 0) {
  console.error(`\n${failed} schema(s) failed validation`);
  process.exit(1);
}
console.log(`\n${files.length} schema(s) valid`);
