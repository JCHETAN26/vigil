# Vigil dashboard

Defaults: `npm run dev` / `npm start` serve on `127.0.0.1:3000` and call the API at
`http://127.0.0.1:8080` — the engine's default port and default CORS origins
(`http://127.0.0.1:3000`, `http://localhost:3000`), so `make dev` works with no configuration.

## Running on other ports

Override both sides; the defaults stay untouched. For a UI on 3308 against an API on 8087:

```sh
# engine/: allow the UI's origin (the list replaces the defaults; see engine/TRIAGE_API.md)
VIGIL_API_PORT=8087 VIGIL_DASHBOARD_ORIGINS=http://127.0.0.1:3308,http://localhost:3308 python -m engine.api

# dashboard/
NEXT_PUBLIC_API_URL=http://127.0.0.1:8087 npx next dev -H 127.0.0.1 -p 3308
```

Add `NEXT_DIST_DIR=.next-live` to the dashboard command to keep that server's build out of `.next`
(the dir is gitignored and excluded from `npm run typecheck`). Next may add
`.next-live/types/**/*.ts` to `tsconfig.json`'s `include` when it starts; don't commit that line.

## Optional same-origin API proxy (one port over SSH)

With `NEXT_PUBLIC_API_URL=/api/vigil` the browser calls the dashboard itself, and
`app/api/vigil/[...path]/route.ts` forwards each GET (query unchanged, no cookies or request
headers) to `VIGIL_API_PROXY_TARGET`, read on the server only. The target must be a loopback
http origin such as `http://127.0.0.1:8080`; anything else is refused, and with it unset the
proxy answers 404. Direct calls (`NEXT_PUBLIC_API_URL=http://127.0.0.1:8080`, the default) and
the API's CORS allow-list are unchanged. `make demo` uses the proxy, so forwarding the dashboard
port is enough (`ssh -N -L 3318:127.0.0.1:3318 <ssh-host>`).

`NEXT_PUBLIC_VIGIL_DEMO=1` (set by `make demo`) shows the demo notice on every page; runs whose
suite starts with `vigil-demo-synthetic-` are badged SYNTHETIC wherever they appear.

## Browser tests

Live Playwright tests take `PLAYWRIGHT_BASE_URL` (default `http://127.0.0.1:3000`). The
route-mocked suite (`npm run e2e:mocked`) starts its own server on port 3317 (`VIGIL_MOCK_PORT`)
and never talks to a real API; its server runs with the demo notice on and the proxy pointed at
127.0.0.1:3319 (`VIGIL_MOCK_PROXY_PORT`), where `e2e/mocked/demo.spec.ts` starts a stand-in
upstream.
