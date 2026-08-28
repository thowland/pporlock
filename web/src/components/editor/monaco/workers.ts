/**
 * Monaco's web workers, bundled from the local `monaco-editor` package.
 *
 * The `?worker` imports make Vite emit each worker as its own local chunk. This
 * is the whole reason Monaco can be used here at all: the default distribution
 * pulls its workers from a CDN at runtime, and this page has no network beyond
 * the daemon origin (SPEC-2 §2.3). Nothing in this file may reference an
 * absolute URL.
 */
import EditorWorker from 'monaco-editor/esm/vs/editor/editor.worker?worker';

interface MonacoGlobal {
  MonacoEnvironment?: { getWorker: (workerId: string, label: string) => Worker };
}

export function installMonacoEnvironment(): void {
  // Only the core editor worker is wired: we ship no TypeScript, JSON, CSS or
  // HTML language services, because the two languages here are YAML and Python
  // and their validation comes from the daemon (`POST /validate`).
  (globalThis as unknown as MonacoGlobal).MonacoEnvironment = {
    getWorker: () => new EditorWorker(),
  };
}
