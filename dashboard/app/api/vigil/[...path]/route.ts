// Optional same-origin proxy to the read-only Vigil API: with NEXT_PUBLIC_API_URL=/api/vigil the
// browser only ever talks to the dashboard's own port (one SSH forward is enough), and this
// handler forwards each GET, query string unchanged, to the fixed loopback VIGIL_API_PROXY_TARGET
// (lib/proxyTarget.ts). GET only; no request headers or cookies are passed on. Off when unset.

import { proxyTarget, safeSegments } from "@/lib/proxyTarget";

export const dynamic = "force-dynamic";

const fail = (status: number, detail: string) =>
  new Response(JSON.stringify({ detail }), {
    status,
    headers: { "content-type": "application/json", "cache-control": "no-store" },
  });

export async function GET(req: Request, ctx: { params: Promise<{ path: string[] }> }) {
  const target = proxyTarget(process.env.VIGIL_API_PROXY_TARGET);
  if (!target.ok) return fail(target.status, target.reason);
  const { path } = await ctx.params;
  if (!safeSegments(path)) return fail(400, "unsupported API path");
  const upstream = `${target.base}/${path.join("/")}${new URL(req.url).search}`;
  let resp: Response;
  try {
    resp = await fetch(upstream, {
      headers: { accept: "application/json" },
      cache: "no-store",
      redirect: "manual",
    });
  } catch {
    return fail(502, `the dashboard proxy cannot reach the Vigil API at ${target.base}`);
  }
  return new Response(await resp.arrayBuffer(), {
    status: resp.status,
    headers: {
      "content-type": resp.headers.get("content-type") ?? "application/json",
      "cache-control": "no-store",
    },
  });
}
