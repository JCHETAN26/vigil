"use client";

import { useCallback, useEffect, useRef, useState } from "react";

// A tiny data-fetching hook that gives every page the same loading / error / (data) states.
// Every request (the effect's or a reload) takes a sequence number and only the latest may set
// state. A dependency change or unmount bumps the sequence, so an older request — e.g. a Retry
// still in flight when the selection changed — can never land after a newer one.
export function useAsync<T>(fn: () => Promise<T>, deps: unknown[]) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState(true);
  const seq = useRef(0);

  const run = useCallback(() => {
    const id = ++seq.current;
    const latest = () => seq.current === id;
    setLoading(true);
    setError(null);
    // new Promise turns a synchronous throw in fn into a rejection, handled like any other.
    new Promise<T>((resolve) => resolve(fn()))
      .then(
        (d) => {
          if (latest()) setData(d);
        },
        (e: unknown) => {
          if (latest()) setError(e instanceof Error ? e : new Error(String(e)));
        },
      )
      .finally(() => {
        if (latest()) setLoading(false);
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  useEffect(() => {
    run();
    return () => {
      seq.current++;
    };
  }, [run]);
  // Wrapped so a click event passed by onRetry never reaches run.
  const reload = useCallback(() => run(), [run]);
  return { data, error, loading, reload };
}

// useAsync, but the result is tagged with the key it answers. A response (or error) for an older
// key is never shown — not even while the new key loads — and `key: null` means "don't fetch".
export function useKeyed<T>(key: string | null, fn: (key: string) => Promise<T>) {
  const r = useAsync(
    useCallback(
      () =>
        key == null
          ? Promise.resolve(null)
          : fn(key).then(
              (data) => ({ key, data, error: null }),
              (e: unknown) => ({ key, data: null, error: e instanceof Error ? e : new Error(String(e)) }),
            ),
      // eslint-disable-next-line react-hooks/exhaustive-deps
      [key],
    ),
    [key],
  );
  const cur = !r.loading && r.data && r.data.key === key ? r.data : null;
  return { data: cur?.data ?? null, error: cur?.error ?? null, loading: key != null && !cur, reload: r.reload };
}

export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <div role="status" aria-live="polite" className="p-6 text-sm opacity-70">
      {label}
    </div>
  );
}

export function EmptyState({ label = "Nothing here yet." }: { label?: string }) {
  return (
    <div role="status" className="p-6 text-sm opacity-70 border border-dashed rounded-md">
      {label}
    </div>
  );
}

export function ErrorBox({ error, onRetry }: { error: Error; onRetry?: () => void }) {
  return (
    <div role="alert" className="p-4 my-4 border border-red-600 rounded-md text-sm">
      <p className="font-semibold text-red-700">Something went wrong</p>
      <p className="mt-1 break-words">{error.message}</p>
      {onRetry && (
        <button
          onClick={onRetry}
          className="mt-3 px-3 py-1 border rounded-md focus-visible:outline"
          type="button"
        >
          Retry
        </button>
      )}
    </div>
  );
}
