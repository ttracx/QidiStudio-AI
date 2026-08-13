"use client";

/**
 * THOX Forger — Print Health.
 *
 * Live defect monitoring and closed-loop control for the running job. Drop into
 * `thox-forge/packages/web/app/print-health/` and set
 * `NEXT_PUBLIC_THOX_AGENT_URL` to the sidecar.
 *
 * The editorial rule this screen is built around: **show what the system cannot
 * see as prominently as what it can.** A panel that renders a calm green tick
 * when no classifier is configured, or when the camera is dark, manufactures
 * confidence the system has not earned — and an operator who trusts it stops
 * walking past the printer. So capability gaps and camera faults are rendered
 * at the same weight as detections, and a refused action is presented as advice
 * rather than as an error, because in that case the printer was never touched.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Activity,
  AlertTriangle,
  Ban,
  CameraOff,
  CheckCircle2,
  Eye,
  Loader2,
  Pause,
  Play,
  RefreshCw,
  ShieldAlert,
  Wrench,
} from "lucide-react";

const BASE = (
  process.env.NEXT_PUBLIC_THOX_AGENT_URL ?? "http://127.0.0.1:7861"
).replace(/\/$/, "");

type Urgency = "critical" | "serious" | "cosmetic" | "observational";

interface Detection {
  kind: string;
  label: string;
  urgency: Urgency;
  confidence: number;
  severity: number;
  note: string;
  bbox_norm: number[] | null;
}

interface HealthState {
  running: boolean;
  autonomy: string;
  watching: string;
  samples: number;
  last_error: string;
  frame_age_s: number | null;
  job: {
    state?: string;
    filename?: string;
    progress?: number;
    current_layer?: number | null;
    total_layer?: number | null;
  };
  verdict: {
    severity: number;
    confidence: number;
    urgency: Urgency;
    summary: string;
    camera_fault: string;
    detections: Detection[];
    voted: string[];
    skipped: string[];
  } | null;
  suspicions: { kind: string; label: string; count: number; needed: number }[];
  ensemble: { active: string[]; has_classifier: boolean; sends_frames_offsite: string[] };
  thresholds: { auto_pause_confidence: number; confirm_frames: number };
}

interface ThoxEvent {
  seq: number;
  at: number;
  kind: string;
  message: string;
  severity: number;
  notable: boolean;
}

/** A refused action. The printer was NOT touched. */
class Refusal extends Error {
  constructor(readonly reason: string, message: string) {
    super(message);
  }
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, {
      ...init,
      headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    });
  } catch (cause) {
    throw new Error(
      `Cannot reach the THOX service at ${BASE}. Start it with ` +
        `"python -m thox.app --port 7862".`,
      { cause },
    );
  }
  if (response.status === 409) {
    const body = (await response.json().catch(() => ({}))) as {
      reason?: string;
      message?: string;
      error?: string;
    };
    throw new Refusal(
      body.reason ?? "refused",
      body.message ?? body.error ?? "the printer declined this action",
    );
  }
  if (!response.ok) throw new Error(`${path} failed: HTTP ${response.status}`);
  return (await response.json()) as T;
}

const severityTone = (value: number) =>
  value >= 0.75
    ? "text-red-400 border-red-400/40 bg-red-400/10"
    : value >= 0.4
      ? "text-amber-400 border-amber-400/40 bg-amber-400/10"
      : "text-emerald-400 border-emerald-400/40 bg-emerald-400/10";

const boxColor = (value: number) =>
  value >= 0.75 ? "#f87171" : value >= 0.4 ? "#fbbf24" : "#34d399";

export default function PrintHealthPage() {
  const [state, setState] = useState<HealthState | null>(null);
  const [events, setEvents] = useState<ThoxEvent[]>([]);
  const [notice, setNotice] = useState<{ text: string; kind: "info" | "error" } | null>(
    null,
  );
  const [busy, setBusy] = useState(false);
  const [frameUrl, setFrameUrl] = useState<string>("");
  const seqRef = useRef(0);

  const poll = useCallback(async () => {
    try {
      const next = await call<HealthState>("/thox/monitor/state");
      setState(next);
      const batch = await call<{ events: ThoxEvent[]; last_seq: number }>(
        `/thox/events?since=${seqRef.current}&limit=200`,
      );
      seqRef.current = batch.last_seq;
      if (batch.events.length) {
        setEvents((previous) =>
          [...batch.events.filter((e) => e.notable).reverse(), ...previous].slice(0, 200),
        );
      }
      // Cache-bust so the browser fetches the newly analyzed frame.
      if (next.running) setFrameUrl(`${BASE}/thox/monitor/frame?t=${Date.now()}`);
    } catch (error) {
      setNotice({
        text: error instanceof Error ? error.message : String(error),
        kind: "error",
      });
    }
  }, []);

  useEffect(() => {
    void poll();
    const timer = setInterval(() => void poll(), 2500);
    return () => clearInterval(timer);
  }, [poll]);

  const act = useCallback(
    async (path: string, label: string, confirm?: string) => {
      if (confirm && !window.confirm(confirm)) return;
      setBusy(true);
      setNotice(null);
      try {
        await call(path, { method: "POST", body: JSON.stringify({ actor: "human" }) });
        setNotice({ text: `${label} sent.`, kind: "info" });
      } catch (error) {
        setNotice({
          text: error instanceof Error ? error.message : String(error),
          // A refusal is advice, not a fault: nothing was sent to the printer.
          kind: error instanceof Refusal ? "info" : "error",
        });
      } finally {
        setBusy(false);
        void poll();
      }
    },
    [poll],
  );

  const printerState = state?.job?.state ?? "unknown";
  const printing = printerState === "printing";
  const paused = printerState === "paused";
  const verdict = state?.verdict ?? null;
  const severity = verdict?.severity ?? 0;

  const worst = useMemo(
    () => verdict?.detections?.find((d) => d.kind !== "camera_fault") ?? null,
    [verdict],
  );

  return (
    <div className="min-h-screen bg-neutral-950 text-neutral-100">
      <main className="mx-auto max-w-6xl px-6 py-8">
        <header className="mb-6 flex items-start justify-between gap-4">
          <div>
            <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
              <Activity className="h-6 w-6 text-[#05A451]" /> Print Health
            </h1>
            <p className="mt-1 text-sm text-neutral-400">
              Live defect monitoring and closed-loop control for the running job.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <span className="rounded-full border border-neutral-800 px-3 py-1 text-xs text-neutral-400">
              autonomy: {state?.autonomy ?? "…"}
            </span>
            <button
              type="button"
              onClick={() =>
                void act(
                  state?.running ? "/thox/monitor/stop" : "/thox/monitor/start",
                  state?.running ? "Stop" : "Start",
                )
              }
              disabled={busy}
              className="flex items-center gap-2 rounded-lg bg-[#05A451] px-4 py-2 text-sm font-medium text-black hover:bg-[#048f47] disabled:opacity-40"
            >
              {busy ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Eye className="h-4 w-4" />
              )}
              {state?.running ? "Stop monitoring" : "Start monitoring"}
            </button>
          </div>
        </header>

        {/* Capability gaps rank with detections, not below them. */}
        {state && !state.ensemble.has_classifier && (
          <Banner tone="amber" icon={<ShieldAlert className="h-5 w-5" />}>
            <strong>Change detection only.</strong> No model is configured to
            classify defects, so this watches for sudden change and unusable frames
            — it will not tell spaghetti from warping. Set{" "}
            <code className="text-amber-300">THOX_OLLAMA_BASE_URL</code> or an API
            key for full detection.
          </Banner>
        )}
        {verdict?.camera_fault && (
          <Banner tone="red" icon={<CameraOff className="h-5 w-5" />}>
            <strong>Cannot judge this print.</strong> {verdict.camera_fault}
          </Banner>
        )}
        {state && state.ensemble.sends_frames_offsite.length > 0 && (
          <Banner tone="neutral" icon={<AlertTriangle className="h-5 w-5" />}>
            Frames are being sent off your network to:{" "}
            {state.ensemble.sends_frames_offsite.join(", ")}.
          </Banner>
        )}

        <div className="grid gap-6 lg:grid-cols-[1fr_340px]">
          <div className="space-y-6">
            <section className="overflow-hidden rounded-xl border border-neutral-800 bg-neutral-900/50">
              <div className="flex items-center justify-between border-b border-neutral-800 px-4 py-3 text-sm">
                <span className="font-medium">Camera + defect overlay</span>
                <span className="text-xs text-neutral-500">
                  {state?.frame_age_s != null
                    ? `${state.frame_age_s}s ago`
                    : "no frame yet"}
                </span>
              </div>
              <div className="relative aspect-[4/3] bg-black">
                {frameUrl ? (
                  <>
                    {/* eslint-disable-next-line @next/next/no-img-element */}
                    <img
                      src={frameUrl}
                      alt="Latest analyzed frame"
                      className="h-full w-full object-contain"
                    />
                    <svg
                      viewBox="0 0 1 1"
                      preserveAspectRatio="none"
                      className="pointer-events-none absolute inset-0 h-full w-full"
                    >
                      {(verdict?.detections ?? [])
                        .filter((d) => d.bbox_norm?.length === 4)
                        .map((d, index) => {
                          const [x0, y0, x1, y1] = d.bbox_norm as number[];
                          return (
                            <rect
                              key={`${d.kind}-${index}`}
                              x={x0}
                              y={y0}
                              width={Math.max(0.001, x1 - x0)}
                              height={Math.max(0.001, y1 - y0)}
                              fill="none"
                              stroke={boxColor(d.severity)}
                              strokeWidth={0.004}
                            />
                          );
                        })}
                    </svg>
                  </>
                ) : (
                  <div className="flex h-full items-center justify-center text-sm text-neutral-600">
                    {state?.running
                      ? "Waiting for the first sample…"
                      : "Monitoring is off"}
                  </div>
                )}
              </div>
            </section>

            <section className="rounded-xl border border-neutral-800 bg-neutral-900/50">
              <div className="border-b border-neutral-800 px-4 py-3 text-sm font-medium">
                Event log
              </div>
              <div className="max-h-72 overflow-y-auto">
                {events.length === 0 ? (
                  <p className="p-4 text-sm text-neutral-500">
                    No notable events yet. Routine samples are recorded but not
                    shown here — otherwise they would bury the entries that matter.
                  </p>
                ) : (
                  <ul className="divide-y divide-neutral-800/60">
                    {events.map((event) => (
                      <li key={event.seq} className="flex gap-3 px-4 py-2 text-xs">
                        <span className="w-16 shrink-0 text-neutral-600">
                          {new Date(event.at * 1000).toLocaleTimeString()}
                        </span>
                        <span
                          className={`w-32 shrink-0 ${
                            event.severity >= 0.6 ? "text-red-400" : "text-[#05A451]"
                          }`}
                        >
                          {event.kind}
                        </span>
                        <span className="text-neutral-300">{event.message}</span>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </section>
          </div>

          <div className="space-y-6">
            <section className="rounded-xl border border-neutral-800 bg-neutral-900/50 p-4">
              <div className="mb-3 flex items-center justify-between">
                <h2 className="text-sm font-medium">Status</h2>
                <button
                  type="button"
                  onClick={() => void poll()}
                  className="text-neutral-500 hover:text-neutral-300"
                  aria-label="Refresh"
                >
                  <RefreshCw className="h-4 w-4" />
                </button>
              </div>

              <div
                className={`mb-3 rounded-lg border p-3 ${
                  verdict?.camera_fault
                    ? "border-red-400/40 bg-red-400/10 text-red-300"
                    : severityTone(severity)
                }`}
              >
                <div className="flex items-center gap-2 text-sm font-medium">
                  {severity > 0 ? (
                    <AlertTriangle className="h-4 w-4" />
                  ) : (
                    <CheckCircle2 className="h-4 w-4" />
                  )}
                  {worst ? worst.label : verdict?.summary || "No defects flagged"}
                </div>
                {worst && (
                  <p className="mt-1 text-xs opacity-80">
                    {worst.urgency} · confidence {(worst.confidence * 100).toFixed(0)}%
                    {worst.note ? ` · ${worst.note}` : ""}
                  </p>
                )}
              </div>

              <dl className="space-y-1.5 text-xs">
                <Row label="Printer" value={printerState} />
                <Row label="Job" value={state?.watching || "—"} />
                <Row
                  label="Layer"
                  value={
                    state?.job?.current_layer != null
                      ? `${state.job.current_layer} / ${state.job.total_layer ?? "?"}`
                      : "—"
                  }
                />
                <Row label="Samples" value={String(state?.samples ?? 0)} />
                <Row
                  label="Voted"
                  value={verdict?.voted?.join(", ") || state?.ensemble.active.join(", ") || "—"}
                />
                {verdict?.skipped && verdict.skipped.length > 0 && (
                  <Row label="Skipped" value={verdict.skipped.join(", ")} warn />
                )}
              </dl>

              {(state?.suspicions?.length ?? 0) > 0 && (
                <div className="mt-3 rounded-lg border border-amber-400/30 bg-amber-400/5 p-2 text-xs text-amber-200">
                  <p className="mb-1 font-medium">Watching (not yet confirmed)</p>
                  {state!.suspicions.map((s) => (
                    <p key={s.kind}>
                      {s.label} — {s.count}/{s.needed} consecutive samples
                    </p>
                  ))}
                </div>
              )}

              {state?.last_error && (
                <p className="mt-3 text-xs text-red-400">{state.last_error}</p>
              )}
            </section>

            <section className="rounded-xl border border-neutral-800 bg-neutral-900/50 p-4">
              <h2 className="mb-3 text-sm font-medium">Controls</h2>
              <div className="grid grid-cols-2 gap-2">
                <Control
                  icon={<Pause className="h-4 w-4" />}
                  label="Pause"
                  disabled={!printing || busy}
                  onClick={() => void act("/thox/control/pause", "Pause")}
                />
                <Control
                  icon={<Play className="h-4 w-4" />}
                  label="Resume"
                  disabled={!paused || busy}
                  onClick={() => void act("/thox/control/resume", "Resume")}
                />
                <Control
                  icon={<Ban className="h-4 w-4" />}
                  label="Cancel"
                  danger
                  disabled={(!printing && !paused) || busy}
                  onClick={() =>
                    void act(
                      "/thox/control/cancel",
                      "Cancel",
                      "Cancel the running print? This cannot be undone and the part will be lost.",
                    )
                  }
                />
                <Control
                  icon={<Wrench className="h-4 w-4" />}
                  label="Reprint"
                  disabled={printing || busy}
                  onClick={() => void act("/thox/control/reprint", "Reprint")}
                />
              </div>
              <p className="mt-3 text-[11px] leading-snug text-neutral-500">
                The agent can pause at <code>auto_pause</code> autonomy. It can
                never cancel or reprint on its own — those discard hours of work,
                so they stay human decisions at every autonomy level.
              </p>
            </section>

            {notice && (
              <div
                className={`rounded-xl border p-4 text-sm ${
                  notice.kind === "error"
                    ? "border-red-500/40 bg-red-500/10 text-red-200"
                    : "border-amber-500/40 bg-amber-500/10 text-amber-200"
                }`}
              >
                {notice.text}
                {notice.kind === "info" && (
                  <p className="mt-2 text-xs opacity-75">
                    Nothing was sent to the printer.
                  </p>
                )}
              </div>
            )}
          </div>
        </div>
      </main>
    </div>
  );
}

function Row({ label, value, warn }: { label: string; value: string; warn?: boolean }) {
  return (
    <div className="flex justify-between gap-3">
      <dt className="text-neutral-500">{label}</dt>
      <dd className={`text-right ${warn ? "text-amber-400" : "text-neutral-300"}`}>
        {value}
      </dd>
    </div>
  );
}

function Control({
  icon,
  label,
  onClick,
  disabled,
  danger,
}: {
  icon: React.ReactNode;
  label: string;
  onClick: () => void;
  disabled?: boolean;
  danger?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={`flex items-center justify-center gap-2 rounded-lg border px-3 py-2 text-sm transition disabled:opacity-30 ${
        danger
          ? "border-red-500/40 text-red-300 hover:bg-red-500/10"
          : "border-neutral-700 text-neutral-200 hover:bg-neutral-800"
      }`}
    >
      {icon}
      {label}
    </button>
  );
}

function Banner({
  tone,
  icon,
  children,
}: {
  tone: "amber" | "red" | "neutral";
  icon: React.ReactNode;
  children: React.ReactNode;
}) {
  const tones = {
    amber: "border-amber-500/30 bg-amber-500/5 text-amber-200",
    red: "border-red-500/40 bg-red-500/10 text-red-200",
    neutral: "border-neutral-700 bg-neutral-800/40 text-neutral-300",
  } as const;
  return (
    <section className={`mb-4 rounded-xl border p-4 text-sm ${tones[tone]}`}>
      <div className="flex gap-3">
        <span className="mt-0.5 shrink-0">{icon}</span>
        <div>{children}</div>
      </div>
    </section>
  );
}
