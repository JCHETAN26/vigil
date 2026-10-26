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

Live Playwright tests take `PLAYWRIGHT_BASE_URL` (default `http://127.0.0.1:3000`). The
route-mocked suite (`npm run e2e:mocked`) starts its own server on port 3317 (`VIGIL_MOCK_PORT`)
and never talks to a real API.
