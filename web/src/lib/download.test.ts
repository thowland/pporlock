/** Saving a fetched blob as a file. OI-35. */
import { describe, expect, it } from 'vitest';
import { filenameFromDisposition, saveBlob, type SaveDeps } from './download';

function deps(): SaveDeps & { revoked: string[]; clicks: HTMLAnchorElement[] } {
  const revoked: string[] = [];
  const clicks: HTMLAnchorElement[] = [];
  return {
    revoked,
    clicks,
    createObjectURL: () => 'blob:fake-url',
    revokeObjectURL: (url) => void revoked.push(url),
    anchor: () => {
      const a = document.createElement('a');
      a.click = () => void clicks.push(a);
      return a;
    },
  };
}

describe('saveBlob', () => {
  it('clicks an anchor carrying the blob url and the filename', () => {
    const d = deps();
    saveBlob(new Blob(['x']), 'session.har.json', d);

    expect(d.clicks).toHaveLength(1);
    expect(d.clicks[0]!.getAttribute('download')).toBe('session.har.json');
    expect(d.clicks[0]!.getAttribute('href')).toBe('blob:fake-url');
  });

  it('revokes the object url even when the click throws', () => {
    // An object URL pins its blob in memory until released, and a session
    // export is megabytes. Leaking one per click is invisible until the tab has
    // been open a while, and then gets blamed on the daemon.
    const d = deps();
    d.anchor = () => {
      const a = document.createElement('a');
      a.click = () => {
        throw new Error('no');
      };
      return a;
    };

    expect(() => saveBlob(new Blob(['x']), 'f.json', d)).toThrow();
    expect(d.revoked).toEqual(['blob:fake-url']);
  });
});

describe('filenameFromDisposition', () => {
  it('reads the name the daemon asked for', () => {
    // The daemon already sends one; honouring it keeps the name in one place
    // rather than reconstructing it here and letting the two drift.
    expect(filenameFromDisposition('attachment; filename="s1a05.har.json"')).toBe('s1a05.har.json');
  });

  it('falls back to null when there is no header or no name in it', () => {
    expect(filenameFromDisposition(null)).toBeNull();
    expect(filenameFromDisposition('attachment')).toBeNull();
  });

  it('reduces a path to its basename', () => {
    // This value reaches a `download` attribute. The daemon never sends a path,
    // but a header is not a place to accept one on trust.
    expect(filenameFromDisposition('attachment; filename="../../etc/passwd"')).toBe('passwd');
  });
});
