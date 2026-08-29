/**
 * Publish the API contract as documentation (REQ DOC-004).
 *
 * Generated rather than written, for the reason every other generated artefact
 * in this repository is: a hand-maintained copy of a machine-readable contract
 * is a copy that will disagree with it, and the disagreement will be found by
 * someone writing a client against the wrong half. `make docs` regenerates;
 * `make gate` fails if the checked-in output is stale.
 *
 *   node scripts/generate-docs.mjs [--check]
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import YAML from 'yaml';

const HERE = dirname(fileURLToPath(import.meta.url));
const CONTRACTS = resolve(HERE, '..');
const DOCS = resolve(CONTRACTS, '../docs');
const CHECK = process.argv.includes('--check');

const BANNER = `<!-- GENERATED FILE — do not edit.
     Source: contracts/openapi.yaml and contracts/schemas/.
     Regenerate with: make docs -->\n\n`;

const esc = (s) => String(s ?? '').replace(/\|/g, '\\|').replace(/\n+/g, ' ').trim();

/** First sentence, for a table cell. The rest stays in the source. */
function brief(text, limit = 160) {
  const one = esc(text);
  if (one.length <= limit) return one;
  const cut = one.slice(0, limit);
  const stop = cut.lastIndexOf('. ');
  return (stop > 40 ? cut.slice(0, stop + 1) : cut.trimEnd() + '…').trim();
}

function typeOf(schema) {
  if (!schema) return '';
  if (schema.$ref) return `\`${schema.$ref.split('/').pop().replace('.json', '')}\``;
  if (Array.isArray(schema.type)) return `\`${schema.type.join(' \\| ')}\``;
  if (schema.enum) return schema.enum.map((v) => `\`${v}\``).join(' \\| ');
  if (schema.type === 'array') return `array of ${typeOf(schema.items) || '`object`'}`;
  return schema.type ? `\`${schema.type}\`` : '';
}

// ------------------------------------------------------------------ API ---

function apiReference(spec) {
  const out = [BANNER];
  out.push('# Control API reference\n');
  out.push(
    `**REQ API-029, DOC-004.** Generated from \`contracts/openapi.yaml\`, which is the source of truth — this file is a rendering of it, not a second description.\n`,
  );
  out.push(
    `Everything here is served on loopback only, and every route except \`/state/health\` and \`/pair\` requires \`Authorization: Bearer <token>\`. Mutating requests also require an \`X-Pporlock-Client\` header naming the caller (\`ui\`, \`cli\`, \`extension\`, \`mcp\`), which is what makes the audit log meaningful — and what makes unmask refusable for everyone but the UI.\n`,
  );
  out.push(`\n**${spec.info?.title ?? 'pporlock'}** ${spec.info?.version ?? ''}\n`);

  const byTag = new Map();
  for (const [path, item] of Object.entries(spec.paths ?? {})) {
    for (const [method, op] of Object.entries(item)) {
      if (method === 'parameters') continue;
      const tag = (op.tags ?? ['other'])[0];
      if (!byTag.has(tag)) byTag.set(tag, []);
      byTag.get(tag).push({ path, method: method.toUpperCase(), op, item });
    }
  }

  out.push('\n## Routes at a glance\n');
  out.push('| Method | Path | Summary |');
  out.push('|---|---|---|');
  for (const tag of byTag.keys()) {
    for (const { path, method, op } of byTag.get(tag)) {
      out.push(`| \`${method}\` | \`${path}\` | ${brief(op.summary)} |`);
    }
  }

  for (const [tag, ops] of byTag) {
    out.push(`\n---\n\n## ${tag}\n`);
    for (const { path, method, op, item } of ops) {
      out.push(`### \`${method} ${path}\`\n`);
      if (op.summary) out.push(`${op.summary}\n`);
      if (op.description) out.push(`${op.description.trim()}\n`);

      const params = [...(item.parameters ?? []), ...(op.parameters ?? [])];
      if (params.length) {
        out.push('| Parameter | In | Type | Notes |');
        out.push('|---|---|---|---|');
        for (const p of params) {
          const r = p.$ref
            ? (spec.components?.parameters ?? {})[p.$ref.split('/').pop()]
            : p;
          if (!r) continue;
          out.push(
            `| \`${r.name}\` | ${r.in} | ${typeOf(r.schema)} | ${brief(r.description)}${r.required ? ' **required**' : ''} |`,
          );
        }
        out.push('');
      }

      const body = op.requestBody?.content?.['application/json']?.schema;
      if (body) out.push(`**Request body:** ${typeOf(body) || '`object`'}\n`);

      const responses = Object.entries(op.responses ?? {});
      if (responses.length) {
        out.push('| Status | Meaning |');
        out.push('|---|---|');
        for (const [code, r] of responses) out.push(`| \`${code}\` | ${brief(r.description)} |`);
        out.push('');
      }
    }
  }
  out.push(
    '\n---\n\nSee also: [SPEC-0 §6](spec-0-contracts.md) for the normative prose, and [`contracts/openapi.yaml`](../contracts/openapi.yaml) for the machine-readable source.\n',
  );
  return out.join('\n');
}

// --------------------------------------------------------------- schemas ---

function properties(schema, defs, depth = 0) {
  const rows = [];
  const required = new Set(schema.required ?? []);
  for (const [name, prop] of Object.entries(schema.properties ?? {})) {
    const resolved = prop.$ref?.startsWith('#/$defs/')
      ? defs[prop.$ref.split('/').pop()]
      : prop;
    rows.push(
      `| ${'  '.repeat(depth)}\`${name}\` | ${typeOf(resolved) || typeOf(prop)} | ${required.has(name) ? 'yes' : ''} | ${brief(resolved?.description ?? prop.description)} |`,
    );
  }
  return rows;
}

function schemaReference() {
  const rule = JSON.parse(readFileSync(resolve(CONTRACTS, 'schemas/rule.schema.json'), 'utf8'));
  const manifest = JSON.parse(
    readFileSync(resolve(CONTRACTS, 'schemas/module-manifest.schema.json'), 'utf8'),
  );
  const defs = rule.$defs ?? {};

  const out = [BANNER];
  out.push('# Rule and manifest schema reference\n');
  out.push(
    '**REQ MOD-015, DOC-004.** Generated from `contracts/schemas/`. The same JSON Schema validates the module loader, the web UI editor, and the MCP `validate_module` tool, so all three agree on what a valid rule is — which is the point of publishing it rather than describing it.\n',
  );
  out.push(
    'For *how* to use these, read [the module cookbook](module-cookbook.md). This is the field list.\n',
  );

  out.push('\n## Module manifest (`module.yaml`)\n');
  out.push('| Field | Type | Required | Notes |');
  out.push('|---|---|---|---|');
  out.push(...properties(manifest, manifest.$defs ?? {}));
  out.push(
    '\nValidation is strict: an unknown top-level key is an error, not a warning (REQ MOD-014). A typo in a key name is otherwise a setting that silently never applies.\n',
  );

  out.push('\n## Rule\n');
  out.push('| Field | Type | Required | Notes |');
  out.push('|---|---|---|---|');
  out.push(...properties(rule, defs));

  out.push('\n## `match`\n');
  out.push('| Criterion | Type | Notes |');
  out.push('|---|---|---|');
  for (const [name, prop] of Object.entries(defs.match?.properties ?? {})) {
    out.push(`| \`${name}\` | ${typeOf(prop)} | ${brief(prop.description)} |`);
  }
  out.push(
    '\nAll present criteria must match; absent criteria do not constrain. `path` is `re.search`, not `fullmatch` — anchor explicitly when you mean it.\n',
  );

  out.push('\n## Actions\n');
  out.push(
    `Every action: ${(defs.action?.enum ?? []).map((a) => `\`${a}\``).join(', ')}.\n`,
  );
  if (defs.action?.description) out.push(`${defs.action.description}\n`);
  out.push('| Action | Additional fields |');
  out.push('|---|---|');
  for (const branch of rule.allOf ?? []) {
    const name = branch.if?.properties?.action?.const;
    if (!name) continue;
    const fields = Object.keys(branch.then?.properties ?? {});
    const req = new Set(branch.then?.required ?? []);
    out.push(
      `| \`${name}\` | ${fields.map((f) => `\`${f}\`${req.has(f) ? ' **required**' : ''}`).join(', ') || '—'} |`,
    );
  }

  out.push('\n## Transforms\n');
  out.push(
    `Built-in transform kinds: ${(defs.transform_kind?.enum ?? []).map((t) => `\`${t}\``).join(', ')}. Modules add their own with \`ctx.register_transform\`.\n`,
  );
  out.push('| Kind | Parameters |');
  out.push('|---|---|');
  for (const branch of defs.transform?.allOf ?? []) {
    const kind = branch.if?.properties?.kind?.const;
    if (!kind) continue;
    const props = branch.then?.properties ?? {};
    const cells = Object.entries(props).map(([n, p]) => {
      const d = p.default !== undefined ? ` (default \`${JSON.stringify(p.default)}\`)` : '';
      return `\`${n}\`${d}`;
    });
    out.push(`| \`${kind}\` | ${cells.join(', ') || '—'} |`);
  }
  out.push(
    '\nTransforms are named registry entries, never expressions embedded in YAML (REQ MOD-013).\n',
  );

  out.push(
    '\n---\n\nSource: [`contracts/schemas/rule.schema.json`](../contracts/schemas/rule.schema.json) and [`module-manifest.schema.json`](../contracts/schemas/module-manifest.schema.json).\n',
  );
  return out.join('\n');
}

// ------------------------------------------------------------------ main ---

const spec = YAML.parse(readFileSync(resolve(CONTRACTS, 'openapi.yaml'), 'utf8'));
const outputs = [
  [resolve(DOCS, 'api-reference.md'), apiReference(spec)],
  [resolve(DOCS, 'rule-schema.md'), schemaReference()],
];

let stale = 0;
for (const [path, content] of outputs) {
  if (CHECK) {
    let current = '';
    try {
      current = readFileSync(path, 'utf8');
    } catch {
      /* missing counts as stale */
    }
    if (current !== content) {
      console.error(`stale: ${path.replace(resolve(CONTRACTS, '..') + '/', '')}`);
      stale += 1;
    }
  } else {
    writeFileSync(path, content);
    console.log(`wrote ${path.replace(resolve(CONTRACTS, '..') + '/', '')}`);
  }
}

if (CHECK && stale) {
  console.error(`\n${stale} generated document(s) out of date — run 'make docs'`);
  process.exit(1);
}
if (CHECK) console.log('ok   generated documentation is current');
