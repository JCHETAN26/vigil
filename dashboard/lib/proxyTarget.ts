// The optional same-origin API proxy (app/api/vigil/[...path]/route.ts). It forwards GETs to ONE
// fixed backend, VIGIL_API_PROXY_TARGET, read on the server only. The target must be a loopback
// http origin (127.0.0.1, localhost or [::1], any port, no path), so the proxy can never be
// pointed at another host. Unset means the proxy is off.

export type ProxyTarget =
  | { ok: true; base: string }
  | { ok: false; status: number; reason: string };

const LOOPBACK = new Set(["127.0.0.1", "localhost", "[::1]"]);

export function proxyTarget(raw: string | undefined): ProxyTarget {
  if (!raw) {
    return {
      ok: false,
      status: 404,
      reason: "the dashboard API proxy is off (set VIGIL_API_PROXY_TARGET, e.g. http://127.0.0.1:8080)",
    };
  }
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    return { ok: false, status: 500, reason: "VIGIL_API_PROXY_TARGET is not a URL" };
  }
  const bare = url.pathname === "/" && !url.search && !url.hash && !url.username && !url.password;
  if (url.protocol !== "http:" || !LOOPBACK.has(url.hostname) || !bare) {
    return {
      ok: false,
      status: 500,
      reason: "VIGIL_API_PROXY_TARGET must be a loopback http origin such as http://127.0.0.1:8080",
    };
  }
  return { ok: true, base: `${url.protocol}//${url.host}` };
}

// Path segments the API uses (ids, version hashes, endpoint names). Anything else is refused.
export function safeSegments(path: string[]): boolean {
  return path.length > 0 && path.every((s) => /^[A-Za-z0-9_@.-]{1,128}$/.test(s) && s !== "." && s !== "..");
}
