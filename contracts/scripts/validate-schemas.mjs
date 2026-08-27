#!/usr/bin/env node
// Validates every JSON Schema in contracts/schemas/ compiles under draft 2020-12,
// and that each declares $id and $schema. SPEC-0 §1.1 / REQ MOD-015.
import { readdirSync, readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import Ajv2020 from 'ajv/dist/2020.js';
import addFormats from 'ajv-formats';

const schemaDir = join(dirname(fileURLToPath(import.meta.url)), '..', 'schemas');
const ajv = new Ajv2020({ strict: true, allErrors: true });
addFormats(ajv);

const files = readdirSync(schemaDir).filter((f) => f.endsWith('.schema.json'));
if (files.length === 0) {
  console.error('FAIL: no schemas found in contracts/schemas/');
  process.exit(1);
}

let failed = 0;
for (const file of files) {
  const raw = readFileSync(join(schemaDir, file), 'utf8');
  let schema;
  try {
    schema = JSON.parse(raw);
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
