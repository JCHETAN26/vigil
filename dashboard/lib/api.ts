// Client for the read-only dashboard API. Base URL from NEXT_PUBLIC_API_URL (default the
// local API on 127.0.0.1); every call is a GET.

import type {
  CaseRow,
  CompareResult,
  RunDetail,
  RunSummary,
  TraceDetail,
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
  getCases: (id: string, limit = 100, offset = 0) =>
    get<{ run_id: string; cases: CaseRow[]; limit: number; offset: number }>(
      `/runs/${id}/cases?limit=${limit}&offset=${offset}`,
    ),
  getTrace: (traceId: string) => get<TraceDetail>(`/traces/${traceId}`),
  compare: (baseline: string, candidate: string) =>
    get<CompareResult>(
      `/compare?baseline=${encodeURIComponent(baseline)}&candidate=${encodeURIComponent(candidate)}`,
    ),
};
