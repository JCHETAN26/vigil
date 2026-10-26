// Client for the read-only dashboard API. Base URL from NEXT_PUBLIC_API_URL (default the
// local API on 127.0.0.1); every call is a GET. `/api/vigil` selects the optional same-origin
// proxy (app/api/vigil/[...path]/route.ts), which forwards to VIGIL_API_PROXY_TARGET.

import type {
  CaseRow,
  CompareCasesResult,
  CompareResult,
  RunDetail,
  RunSummary,
  RunVersionsPage,
  TraceDetail,
  VersionDiff,
} from "./types";

const BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8080";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function get<T>(path: string): Promise<T> {
  let resp: Response;
  try {
    resp = await fetch(`${BASE}${path}`, { headers: { accept: "application/json" } });
  } catch (e) {
    throw new ApiError(0, `cannot reach the API at ${BASE} (${String(e)})`);
  }
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const body = await resp.json();
      if (body && typeof body.detail === "string") detail = body.detail;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(resp.status, detail);
  }
  return (await resp.json()) as T;
}

export const api = {
  listRuns: (limit = 50, offset = 0) =>
    get<{ runs: RunSummary[]; limit: number; offset: number }>(
      `/runs?limit=${limit}&offset=${offset}`,
    ),
  getRun: (id: string) => get<RunDetail>(`/runs/${id}`),
  // Bounded (the API caps limit at 200); `total` tells whether more versions exist.
  getRunVersions: (id: string, limit = 50, offset = 0) =>
    get<RunVersionsPage>(`/runs/${id}/versions?limit=${limit}&offset=${offset}`),
  getCases: (id: string, limit = 100, offset = 0) =>
    get<{ run_id: string; cases: CaseRow[]; limit: number; offset: number }>(
      `/runs/${id}/cases?limit=${limit}&offset=${offset}`,
    ),
  getTrace: (traceId: string) => get<TraceDetail>(`/traces/${traceId}`),
  // The triage calls take a pre-built query (lib/compareQuery.ts) so summary, cases and diff
  // always carry the same run/version selection.
  compare: (query: URLSearchParams) => get<CompareResult>(`/compare?${query}`),
  compareCases: (query: URLSearchParams) => get<CompareCasesResult>(`/compare/cases?${query}`),
  versionDiff: (query: URLSearchParams) => get<VersionDiff>(`/versions/diff?${query}`),
};
