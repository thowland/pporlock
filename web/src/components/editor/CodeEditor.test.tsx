import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { CodeEditor } from './CodeEditor';
import { PlainEditor, MarkerList } from './PlainEditor';
import type { CodeEditorProps } from './types';

afterEach(() => vi.restoreAllMocks());

const MARKERS: CodeEditorProps['markers'] = [
  { line: 12, column: 3, message: 'unknown key: prioriy', severity: 'error', code: 'schema' },
  { line: 4, column: 1, message: 'deprecated transform', severity: 'warning' },
];

describe('PlainEditor', () => {
  it('is a named control and reports edits  # REQ WUI-015', async () => {
    const onChange = vi.fn();
    render(
      <PlainEditor
        value="name: x"
        language="yaml"
        markers={[]}
        onChange={onChange}
        ariaLabel="module.yaml editor"
      />,
    );
    const area = screen.getByLabelText('module.yaml editor');
    await userEvent.type(area, '!');
    expect(onChange).toHaveBeenCalledWith('name: x!');
  });

  it('renders validation findings keyed by line and column  # REQ API-027', () => {
    render(
      <PlainEditor
        value=""
        language="yaml"
        markers={MARKERS}
        onChange={vi.fn()}
        ariaLabel="module.yaml editor"
      />,
    );
    const problems = screen.getByLabelText('Validation problems');
    expect(problems.textContent).toContain('12:3');
    expect(problems.textContent).toContain('schema');
    expect(problems.textContent).toContain('unknown key: prioriy');
    expect(problems.textContent).toContain('4:1');
  });

  it('renders nothing when there are no markers', () => {
    const { container } = render(<MarkerList markers={[]} />);
    expect(container.firstChild).toBeNull();
  });
});

describe('CodeEditor', () => {
  const props: CodeEditorProps = {
    value: 'name: x',
    language: 'yaml',
    markers: [],
    onChange: vi.fn(),
    ariaLabel: 'module.yaml editor',
  };

  it('shows the plain editor until the lazy implementation resolves', async () => {
    let resolve: (component: typeof PlainEditor) => void = () => {};
    const pending = new Promise<typeof PlainEditor>((r) => {
      resolve = r;
    });
    render(<CodeEditor {...props} load={() => pending} />);
    expect(screen.getByLabelText('module.yaml editor').tagName).toBe('TEXTAREA');

    resolve(() => <div data-testid="monaco-stub" />);
    await waitFor(() => expect(screen.getByTestId('monaco-stub')).toBeTruthy());
  });

  it('stays on the plain editor when Monaco fails to load', async () => {
    // A page served from loopback with no CDN fallback must degrade to a usable
    // editor rather than to an empty box (SPEC-2 §2.3).
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    render(<CodeEditor {...props} load={() => Promise.reject(new Error('chunk failed'))} />);
    await waitFor(() => expect(warn).toHaveBeenCalled());
    expect(screen.getByLabelText('module.yaml editor').tagName).toBe('TEXTAREA');
  });
});
