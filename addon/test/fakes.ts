// A recording stand-in for Google's CardService: every newXxx() returns a chainable object that
// remembers the calls made on it, so a test can read back what a card would show.

export class Recorder {
  readonly calls: Array<[string, unknown[]]> = [];
  constructor(readonly kind: string) {}
}

export function makeRecorder(kind: string): Recorder {
  const rec = new Recorder(kind);
  const proxy: Recorder = new Proxy(rec, {
    get(target, prop) {
      if (prop in target || typeof prop === "symbol") return (target as never)[prop];
      return (...args: unknown[]) => {
        target.calls.push([String(prop), args]);
        return prop === "build" ? target : proxy;
      };
    },
  });
  return proxy;
}

export const fakeCardService = new Proxy({}, { get: (_t, name) => () => makeRecorder(String(name)) });

/** Every string passed to any call, at any depth, in the order made. */
export function texts(node: unknown): string[] {
  if (typeof node === "string") return [node];
  if (node instanceof Recorder) return node.calls.flatMap(([, args]) => args.flatMap(texts));
  return [];
}

/** How many recorders of this kind (e.g. "newCardSection") appear anywhere inside the node. */
export function count(node: unknown, kind: string): number {
  if (!(node instanceof Recorder)) return 0;
  return (node.kind === kind ? 1 : 0) + node.calls.flatMap(([, args]) => args).reduce((n: number, a) => n + count(a, kind), 0);
}

/** Whether any call with this method name was made anywhere inside the node. */
export function called(node: unknown, method: string): boolean {
  if (!(node instanceof Recorder)) return false;
  return node.calls.some(([name, args]) => name === method || args.some((a) => called(a, method)));
}
