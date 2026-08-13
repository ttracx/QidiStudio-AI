"use client";

/**
 * Typed client for the thox-q2-vision-scan service.
 *
 * Mirrors the idiom of `lib/thoxcore-client.ts`: a thin typed surface over
 * fetch, with the base URL read from the environment so the scan service can
 * live on the operator's workstation while Forger runs anywhere.
 *
 * The one shape worth calling out is `ScanRefusal`. The service answers a
 * declined interlock with **409** and a machine-readable `reason`, because the
 * printer was not touched and the operator can clear the condition themselves.
 * Collapsing that into a generic error would leave the UI showing "scan failed"
 * when the truthful message is "finish your print first". `startScan` therefore
 * throws a distinguishable error type rather than a string.
 */

export type RefusalCode =
  | "printer_busy"
  | "not_homed"
  | "too_hot"
  | "unsafe_pose"
  | "scan_in_progress"
  | "interlock";

export type Reliability = "good" | "marginal" | "unreliable";
export type ScanTier = "P" | "H" | "F";

export interface PrinterStatus {
  host: string;
  klipper_state: string;
  hostname: string;
  cameras: { name: string; snapshot_url: string; stream_url: string }[];
  can_scan: boolean;
  reason: string;
  refusal_code: string;
}

export interface Health {
  ok: boolean;
  scanning: boolean;
  calibration: {
    provisional: boolean;
    residual_px: number;
    mm_per_px_at_z50: number;
  };
  vision: {
    active: string[];
    inactive: Record<string, string>;
    can_identify_objects: boolean;
  };
}

export interface Pose {
  index: number;
  z_mm: number;
  azimuth_index: number;
  azimuth_deg: number;
}

export interface ScanPlan {
  tier: ScanTier;
  tier_label: string;
  summary: string;
  provisional_calibration: boolean;
  dimensionally_reliable: boolean;
  poses: Pose[];
}

export interface Measurement {
  name: string;
  value_mm: number;
  tolerance_mm: number;
  method: string;
  reliability: Reliability;
}

export interface ScanSession {
  session_id: string;
  state: string;
  tier: ScanTier;
  reliability: Reliability;
  poses: Pose[];
  frames: { filename: string; actual_z_mm: number }[];
  hypotheses: { label: string; confidence: number; source: string }[];
  measurements: {
    width_mm: Measurement;
    depth_mm: Measurement;
    height_mm: Measurement;
    footprint_area_mm2: number;
  } | null;
  artifacts: { kind: string; path: string; bytes: number; note: string }[];
  caveats: string[];
  error: string;
  provider_reports: {
    provider: string;
    role: string;
    ok: boolean;
    elapsed_ms: number;
    confidence: number;
    skipped_reason: string;
  }[];
}

export interface ScanRequest {
  center_x_mm: number;
  center_y_mm: number;
  footprint_radius_mm: number;
  object_height_mm: number;
  stations: number;
  azimuths: number;
  make_tray: boolean;
}

export interface ProgressEvent {
  stage: string;
  index?: number;
  total?: number;
  z?: number;
  reliability?: string;
  prompt?: string;
  [key: string]: unknown;
}

/** A declined interlock. The printer was NOT touched. */
export class ScanRefusal extends Error {
  readonly code: RefusalCode;
  readonly sessionId?: string;

  constructor(code: RefusalCode, message: string, sessionId?: string) {
    super(message);
    this.name = "ScanRefusal";
    this.code = code;
    this.sessionId = sessionId;
  }
}

const BASE = (
  process.env.NEXT_PUBLIC_THOX_SCAN_URL ?? "http://127.0.0.1:8712"
).replace(/\/$/, "");

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, {
      ...init,
      headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    });
  } catch (cause) {
    throw new Error(
      `Cannot reach the scan service at ${BASE}. Start it with ` +
        `"uvicorn thox_scan.service:app --port 8712".`,
      { cause },
    );
  }

  if (response.status === 409) {
    const body = (await response.json().catch(() => ({}))) as {
      detail?: { reason?: RefusalCode; message?: string; session_id?: string };
    };
    throw new ScanRefusal(
      body.detail?.reason ?? "interlock",
      body.detail?.message ?? "the printer declined the scan",
      body.detail?.session_id,
    );
  }
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(`${path} failed: HTTP ${response.status} ${text.slice(0, 200)}`);
  }
  return (await response.json()) as T;
}

export const scanApi = {
  health: () => request<Health>("/health"),
  printer: () => request<PrinterStatus>("/printer"),

  plan: (body: ScanRequest) =>
    request<ScanPlan>("/plan", { method: "POST", body: JSON.stringify(body) }),

  /** Runs to completion; can take several minutes. Subscribe to events first. */
  startScan: (body: ScanRequest) =>
    request<ScanSession>("/scan", { method: "POST", body: JSON.stringify(body) }),

  sessions: (limit = 20) =>
    request<
      {
        session_id: string;
        state: string;
        tier: ScanTier;
        reliability: Reliability;
        frames: number;
        created_at: string;
        dimensions_mm: number[] | null;
      }[]
    >(`/sessions?limit=${limit}`),

  session: (id: string) => request<ScanSession>(`/sessions/${id}`),

  artifactUrl: (id: string, kind: string) =>
    `${BASE}/sessions/${id}/artifact/${kind}`,

  frameUrl: (id: string, index: number) => `${BASE}/sessions/${id}/frame/${index}`,

  /** Live camera stream, served by the printer itself rather than this API. */
  liveStreamUrl: (printer: PrinterStatus | null) =>
    printer?.cameras[0]?.stream_url ?? "",

  /**
   * Subscribe to scan progress. Returns an unsubscribe function.
   *
   * EventSource reconnects on its own, so no retry logic is needed here; the
   * service sends a `retry:` hint and periodic keepalive comments to survive
   * proxies that would otherwise close an idle stream.
   */
  subscribe(onEvent: (event: ProgressEvent) => void): () => void {
    const source = new EventSource(`${BASE}/events`);
    source.onmessage = (message) => {
      try {
        onEvent(JSON.parse(message.data) as ProgressEvent);
      } catch {
        /* a malformed frame must not kill the stream */
      }
    };
    return () => source.close();
  },
};

/** Tailwind classes for a reliability grade. Red is not decoration here. */
export function reliabilityClass(value: Reliability): string {
  switch (value) {
    case "good":
      return "text-emerald-400 border-emerald-400/40 bg-emerald-400/10";
    case "marginal":
      return "text-amber-400 border-amber-400/40 bg-amber-400/10";
    default:
      return "text-red-400 border-red-400/40 bg-red-400/10";
  }
}

export function formatMeasurement(m: Measurement): string {
  return `${m.value_mm.toFixed(1)} ± ${m.tolerance_mm.toFixed(1)} mm`;
}
