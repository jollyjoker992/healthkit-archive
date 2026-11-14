// Error responses (spec/README.md section 6.2).

export type ErrorCode =
  | "bad_request" | "hash_mismatch" | "bad_signature" | "unknown_key" | "stale_timestamp"
  | "device_inactive" | "device_purged" | "not_granted" | "forbidden" | "invite_invalid" | "not_found"
  | "invite_used" | "device_exists" | "user_purged" | "grant_conflict" | "grant_out_of_order"
  | "grant_unknown" | "grant_mismatch" | "seq_conflict" | "seq_out_of_window" | "batch_too_large"
  | "quota_exceeded" | "quota_total_exceeded" | "rate_limited" | "internal";

const STATUS: Record<ErrorCode, number> = {
  bad_request: 400, hash_mismatch: 400,
  bad_signature: 401, unknown_key: 401, stale_timestamp: 401,
  device_inactive: 403, device_purged: 403, not_granted: 403, forbidden: 403, invite_invalid: 403,
  not_found: 404,
  invite_used: 409, device_exists: 409, user_purged: 409, grant_conflict: 409, grant_out_of_order: 409,
  grant_unknown: 409, grant_mismatch: 409, seq_conflict: 409, seq_out_of_window: 409,
  batch_too_large: 413, quota_exceeded: 413, quota_total_exceeded: 413,
  rate_limited: 429, internal: 500,
};

export class HttpError extends Error {
  constructor(
    readonly code: ErrorCode,
    message: string,
    readonly extra: Record<string, unknown> = {},
    readonly headers: Record<string, string> = {},
  ) {
    super(message);
  }
}

export function fail(code: ErrorCode, message: string, extra: Record<string, unknown> = {}, headers: Record<string, string> = {}): never {
  throw new HttpError(code, message, extra, headers);
}

export function json(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store", ...headers },
  });
}

export function errorResponse(err: HttpError, nowSeconds: number): Response {
  const status = STATUS[err.code];
  const extra = status === 401 ? { ...err.extra, server_time: nowSeconds } : err.extra;
  return json({ error: err.code, message: err.message, ...extra }, status, err.headers);
}
