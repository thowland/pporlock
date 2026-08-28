import { createServer } from 'node:net';

/**
 * A port the OS says is free, rather than one chosen at random and hoped for.
 *
 * The specs previously each picked `base + random(400)`, and two of those
 * ranges overlapped. The result was a suite that passed every time a file was
 * run alone and failed occasionally when the whole thing ran — the worst shape
 * a test failure can have, because the natural response is to re-run it and
 * conclude it was nothing.
 *
 * Binding to port 0 and reading back what the kernel assigned leaves a small
 * window between close and re-bind, but it draws from the ephemeral range the
 * OS is already tracking rather than from a guess, so two callers cannot
 * collide the way two random pickers in overlapping ranges will.
 */
export async function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const server = createServer();
    server.unref();
    server.on('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const address = server.address();
      if (address === null || typeof address === 'string') {
        server.close(() => reject(new Error('no port assigned')));
        return;
      }
      const { port } = address;
      server.close(() => resolve(port));
    });
  });
}

/** Several at once, all distinct. */
export async function freePorts(count: number): Promise<number[]> {
  const ports: number[] = [];
  while (ports.length < count) {
    const port = await freePort();
    if (!ports.includes(port)) ports.push(port);
  }
  return ports;
}
