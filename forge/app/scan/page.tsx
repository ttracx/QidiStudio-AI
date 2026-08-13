"use client";

/**
 * THOX Forger — Scan to Print.
 *
 * Place an object on the bed, scan it, get a printable plate.
 *
 * The screen is built around one editorial decision: **the limits of this rig
 * are shown before the button, not after the result.** The Q2 has a single
 * fixed camera and a bed that only moves in Z, so a one-pass scan cannot see
 * the far side of an object and its depth measurement is not trustworthy. A UI
 * that renders "27.0 mm" in the same typeface whether the number is good to
 * 2 mm or wrong by 27 mm is actively misleading, so reliability is a visible
 * badge on every measurement and the coverage choice is the first control the
 * operator meets — not an advanced setting.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  Boxes,
  Camera,
  CheckCircle2,
  CircleSlash,
  Download,
  Layers3,
  Loader2,
  RefreshCw,
  RotateCw,
  Ruler,
  ScanLine,
  ShieldCheck,
} from "lucide-react";
import { IntegrationNav } from "@/components/IntegrationNav";
import { ThoxLogo } from "@/components/ThoxLogo";
import {
  formatMeasurement,
  reliabilityClass,
  ScanRefusal,
  scanApi,
  type Health,
  type Measurement,
  type PrinterStatus,
  type ProgressEvent,
  type ScanPlan,
  type ScanRequest,
  type ScanSession,
} from "@/lib/scan-client";

const DEFAULTS: ScanRequest = {
  center_x_mm: 135,
  center_y_mm: 110,
  footprint_radius_mm: 40,
  object_height_mm: 40,
  stations: 12,
  azimuths: 1,
  make_tray: true,
};

type Phase = "idle" | "planning" | "scanning" | "done" | "refused" | "error";

export default function ScanToPrintPage() {
  const [health, setHealth] = useState<Health | null>(null);
  const [printer, setPrinter] = useState<PrinterStatus | null>(null);
  const [request, setRequest] = useState<ScanRequest>(DEFAULTS);
  const [plan, setPlan] = useState<ScanPlan | null>(null);
  const [session, setSession] = useState<ScanSession | null>(null);
  const [phase, setPhase] = useState<Phase>("idle");
  const [events, setEvents] = useState<ProgressEvent[]>([]);
  const [message, setMessage] = useState<string>("");
  const logRef = useRef<HTMLDivElement>(null);

  const refresh = useCallback(async () => {
    try {
      const [h, p] = await Promise.all([scanApi.health(), scanApi.printer()]);
      setHealth(h);
      setPrinter(p);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = setInterval(() => void refresh(), 8000);
    return () => clearInterval(timer);
  }, [refresh]);

  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight });
  }, [events]);

  const doPlan = useCallback(async () => {
    setPhase("planning");
    setMessage("");
    try {
      setPlan(await scanApi.plan(request));
      setPhase("idle");
    } catch (error) {
      setPlan(null);
      setPhase(error instanceof ScanRefusal ? "refused" : "error");
      setMessage(error instanceof Error ? error.message : String(error));
    }
  }, [request]);

  const doScan = useCallback(async () => {
    setPhase("scanning");
    setEvents([]);
    setSession(null);
    setMessage("");
    const unsubscribe = scanApi.subscribe((event) =>
      setEvents((previous) => [...previous, event]),
    );
    try {
      const result = await scanApi.startScan(request);
      setSession(result);
      setPhase(result.state === "complete" ? "done" : "error");
      if (result.error) setMessage(result.error);
    } catch (error) {
      setPhase(error instanceof ScanRefusal ? "refused" : "error");
      setMessage(error instanceof Error ? error.message : String(error));
    } finally {
      unsubscribe();
    }
  }, [request]);

  const progress = useMemo(() => {
    const frames = events.filter((e) => e.stage === "frame");
    const last = frames.at(-1);
    if (!last?.total) return 0;
    return Math.round((Number(last.index) / Number(last.total)) * 100);
  }, [events]);

  const rotationPrompt = useMemo(
    () => events.filter((e) => e.stage === "await_rotation").at(-1)?.prompt,
    [events],
  );

  const busy = phase === "scanning" || phase === "planning";
  const blocked = !printer?.can_scan;

  return (
    <div className="min-h-screen bg-neutral-950 text-neutral-100">
      <IntegrationNav />
      <main className="mx-auto max-w-6xl px-6 py-8">
        <header className="mb-8 flex items-start justify-between gap-4">
          <div>
            <div className="mb-2 flex items-center gap-3">
              <ThoxLogo className="h-7 w-auto" />
              <h1 className="text-2xl font-semibold tracking-tight">Scan to Print</h1>
            </div>
            <p className="max-w-2xl text-sm text-neutral-400">
              Place an object on the bed. The printer photographs it from a ladder of
              heights, an ensemble of vision models isolates it, and the result is
              reconstructed into a printable plate.
            </p>
          </div>
          <button
            type="button"
            onClick={() => void refresh()}
            className="flex items-center gap-2 rounded-lg border border-neutral-800 px-3 py-2 text-sm text-neutral-300 hover:bg-neutral-900"
          >
            <RefreshCw className="h-4 w-4" /> Refresh
          </button>
        </header>

        {/* What this rig cannot do. Stated before the controls, deliberately. */}
        <section className="mb-6 rounded-xl border border-amber-500/30 bg-amber-500/5 p-4">
          <div className="flex gap-3">
            <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 text-amber-400" />
            <div className="text-sm text-amber-200/90">
              <p className="mb-1 font-medium text-amber-300">
                What this rig can and cannot measure
              </p>
              <p>
                The Q2 has <strong>one fixed camera</strong> and its bed moves{" "}
                <strong>only in Z</strong>. A single pass therefore never sees the far
                side or underside of an object, and reconstruction is a{" "}
                <em>visual hull</em> — concave features, pockets and undercuts are not
                recovered. On a 40 × 25 × 15 mm test object a single pass measured
                depth at <strong>52 mm against 25 mm of truth</strong>. Four passes with
                manual rotation brought every axis within <strong>2 mm</strong>.
              </p>
            </div>
          </div>
        </section>

        <div className="grid gap-6 lg:grid-cols-[1fr_380px]">
          {/* ---------------- left: live view + progress ---------------- */}
          <div className="space-y-6">
            <section className="overflow-hidden rounded-xl border border-neutral-800 bg-neutral-900/50">
              <div className="flex items-center justify-between border-b border-neutral-800 px-4 py-3">
                <div className="flex items-center gap-2 text-sm font-medium">
                  <Camera className="h-4 w-4 text-[#05A451]" /> Live bed view
                </div>
                <span className="text-xs text-neutral-500">
                  {printer?.cameras[0]?.name ?? "no camera"} · 640×480
                </span>
              </div>
              <div className="relative aspect-[4/3] bg-black">
                {printer?.cameras.length ? (
                  /* eslint-disable-next-line @next/next/no-img-element */
                  <img
                    src={scanApi.liveStreamUrl(printer)}
                    alt="Printer bed"
                    className="h-full w-full object-contain"
                  />
                ) : (
                  <div className="flex h-full items-center justify-center text-sm text-neutral-600">
                    No camera reported by Moonraker
                  </div>
                )}
                {phase === "scanning" && (
                  <div className="absolute inset-x-0 bottom-0 bg-black/70 px-4 py-3">
                    <div className="mb-2 flex items-center justify-between text-xs">
                      <span className="flex items-center gap-2 text-[#05A451]">
                        <ScanLine className="h-3.5 w-3.5 animate-pulse" /> Capturing
                      </span>
                      <span className="text-neutral-400">{progress}%</span>
                    </div>
                    <div className="h-1.5 overflow-hidden rounded-full bg-neutral-800">
                      <div
                        className="h-full rounded-full bg-[#05A451] transition-all"
                        style={{ width: `${progress}%` }}
                      />
                    </div>
                  </div>
                )}
              </div>
            </section>

            {rotationPrompt && phase === "scanning" && (
              <section className="rounded-xl border border-[#05A451]/40 bg-[#05A451]/10 p-4">
                <div className="flex gap-3">
                  <RotateCw className="mt-0.5 h-5 w-5 shrink-0 text-[#05A451]" />
                  <div>
                    <p className="font-medium text-[#05A451]">Your turn</p>
                    <p className="text-sm text-neutral-200">{rotationPrompt}</p>
                  </div>
                </div>
              </section>
            )}

            {events.length > 0 && (
              <section className="rounded-xl border border-neutral-800 bg-neutral-900/50">
                <div className="border-b border-neutral-800 px-4 py-3 text-sm font-medium">
                  Activity
                </div>
                <div ref={logRef} className="max-h-56 overflow-y-auto p-4 font-mono text-xs">
                  {events.map((event, index) => (
                    <div key={index} className="text-neutral-400">
                      <span className="text-[#05A451]">{event.stage}</span>
                      {event.stage === "frame" && (
                        <>
                          {" "}
                          {String(event.index)}/{String(event.total)} · Z
                          {String(event.z)} · {String(event.reliability)}
                        </>
                      )}
                    </div>
                  ))}
                </div>
              </section>
            )}

            {session && <Results session={session} />}
          </div>

          {/* ---------------- right: readiness + controls ---------------- */}
          <div className="space-y-6">
            <Readiness printer={printer} health={health} />

            <section className="rounded-xl border border-neutral-800 bg-neutral-900/50 p-4">
              <h2 className="mb-4 flex items-center gap-2 text-sm font-medium">
                <Layers3 className="h-4 w-4 text-[#05A451]" /> Coverage
              </h2>

              <div className="mb-4 space-y-2">
                {[
                  {
                    value: 1,
                    title: "One pass — preview",
                    detail: "Walk away. Shape only; dimensions NOT reliable.",
                  },
                  {
                    value: 4,
                    title: "Four passes — measured",
                    detail: "You rotate the object 90° three times. ~±2 mm.",
                  },
                ].map((option) => (
                  <label
                    key={option.value}
                    className={`block cursor-pointer rounded-lg border p-3 transition ${
                      request.azimuths === option.value
                        ? "border-[#05A451] bg-[#05A451]/10"
                        : "border-neutral-800 hover:border-neutral-700"
                    }`}
                  >
                    <input
                      type="radio"
                      name="azimuths"
                      className="sr-only"
                      checked={request.azimuths === option.value}
                      onChange={() =>
                        setRequest((r) => ({ ...r, azimuths: option.value }))
                      }
                    />
                    <div className="text-sm font-medium">{option.title}</div>
                    <div className="text-xs text-neutral-400">{option.detail}</div>
                  </label>
                ))}
              </div>

              <NumberField
                label="Max object height"
                hint="Z clearance is reserved from this before anything moves. Over-estimating is the safe error."
                suffix="mm"
                value={request.object_height_mm}
                onChange={(v) => setRequest((r) => ({ ...r, object_height_mm: v }))}
              />
              <NumberField
                label="Z stations per pass"
                hint="More stations sample more viewing angles, at one move each."
                value={request.stations}
                onChange={(v) => setRequest((r) => ({ ...r, stations: v }))}
              />

              <label className="mt-3 flex items-center gap-2 text-sm text-neutral-300">
                <input
                  type="checkbox"
                  checked={request.make_tray}
                  onChange={(e) =>
                    setRequest((r) => ({ ...r, make_tray: e.target.checked }))
                  }
                  className="accent-[#05A451]"
                />
                Also generate a fitted tray
              </label>

              <div className="mt-4 flex gap-2">
                <button
                  type="button"
                  onClick={() => void doPlan()}
                  disabled={busy}
                  className="flex-1 rounded-lg border border-neutral-700 px-3 py-2 text-sm hover:bg-neutral-800 disabled:opacity-50"
                >
                  Preview plan
                </button>
                <button
                  type="button"
                  onClick={() => void doScan()}
                  disabled={busy || blocked}
                  className="flex flex-1 items-center justify-center gap-2 rounded-lg bg-[#05A451] px-3 py-2 text-sm font-medium text-black hover:bg-[#048f47] disabled:opacity-40"
                >
                  {phase === "scanning" ? (
                    <>
                      <Loader2 className="h-4 w-4 animate-spin" /> Scanning
                    </>
                  ) : (
                    <>
                      <ScanLine className="h-4 w-4" /> Start scan
                    </>
                  )}
                </button>
              </div>
              {blocked && (
                <p className="mt-2 text-xs text-amber-400">
                  Scanning is blocked: {printer?.reason}
                </p>
              )}
            </section>

            {plan && (
              <section className="rounded-xl border border-neutral-800 bg-neutral-900/50 p-4 text-sm">
                <h2 className="mb-2 font-medium">
                  Plan · tier {plan.tier} — {plan.tier_label}
                </h2>
                <p className="mb-2 text-xs text-neutral-400">{plan.summary}</p>
                {!plan.dimensionally_reliable && (
                  <p className="text-xs text-amber-400">
                    Single azimuth: depth and height will not be dimensionally
                    reliable.
                  </p>
                )}
                {plan.provisional_calibration && (
                  <p className="mt-1 text-xs text-amber-400">
                    Rig calibration is provisional — dimensions are estimates.
                  </p>
                )}
              </section>
            )}

            {message && (
              <section
                className={`rounded-xl border p-4 text-sm ${
                  phase === "refused"
                    ? "border-amber-500/40 bg-amber-500/10 text-amber-200"
                    : "border-red-500/40 bg-red-500/10 text-red-200"
                }`}
              >
                <div className="mb-1 flex items-center gap-2 font-medium">
                  <CircleSlash className="h-4 w-4" />
                  {phase === "refused" ? "Printer declined" : "Error"}
                </div>
                <p>{message}</p>
                {phase === "refused" && (
                  <p className="mt-2 text-xs opacity-80">
                    Nothing was sent to the printer.
                  </p>
                )}
              </section>
            )}
          </div>
        </div>
      </main>
    </div>
  );
}

function Readiness({
  printer,
  health,
}: {
  printer: PrinterStatus | null;
  health: Health | null;
}) {
  return (
    <section className="rounded-xl border border-neutral-800 bg-neutral-900/50 p-4">
      <h2 className="mb-3 flex items-center gap-2 text-sm font-medium">
        <ShieldCheck className="h-4 w-4 text-[#05A451]" /> Readiness
      </h2>
      <dl className="space-y-2 text-xs">
        <Row
          label="Printer"
          value={printer ? `${printer.hostname} · ${printer.klipper_state}` : "…"}
          ok={printer?.klipper_state === "ready"}
        />
        <Row
          label="Interlock"
          value={printer?.reason ?? "…"}
          ok={Boolean(printer?.can_scan)}
        />
        <Row
          label="Calibration"
          value={
            health
              ? health.calibration.provisional
                ? "provisional (estimates only)"
                : `solved · ${health.calibration.residual_px.toFixed(2)} px RMS`
              : "…"
          }
          ok={Boolean(health && !health.calibration.provisional)}
        />
        <Row
          label="Vision"
          value={health ? health.vision.active.join(", ") || "none" : "…"}
          ok={Boolean(health?.vision.active.length)}
        />
      </dl>
      {health && !health.vision.can_identify_objects && (
        <p className="mt-3 text-xs text-neutral-500">
          No language-model member is configured, so the scan measures extent but
          will not identify what the object is.
        </p>
      )}
      {health && Object.keys(health.vision.inactive).length > 0 && (
        <details className="mt-3">
          <summary className="cursor-pointer text-xs text-neutral-500">
            {Object.keys(health.vision.inactive).length} vision provider(s) inactive
          </summary>
          <ul className="mt-2 space-y-1 text-xs text-neutral-500">
            {Object.entries(health.vision.inactive).map(([name, reason]) => (
              <li key={name}>
                <span className="text-neutral-400">{name}</span>: {reason}
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}

function Row({ label, value, ok }: { label: string; value: string; ok: boolean }) {
  return (
    <div className="flex items-start justify-between gap-3">
      <dt className="text-neutral-500">{label}</dt>
      <dd className="flex items-start gap-1.5 text-right text-neutral-300">
        <span className="max-w-[210px]">{value}</span>
        {ok ? (
          <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0 text-emerald-400" />
        ) : (
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-400" />
        )}
      </dd>
    </div>
  );
}

function Results({ session }: { session: ScanSession }) {
  const measurements: Measurement[] = session.measurements
    ? [
        session.measurements.width_mm,
        session.measurements.depth_mm,
        session.measurements.height_mm,
      ]
    : [];

  return (
    <section className="rounded-xl border border-neutral-800 bg-neutral-900/50">
      <div className="flex items-center justify-between border-b border-neutral-800 px-4 py-3">
        <div className="flex items-center gap-2 text-sm font-medium">
          <Boxes className="h-4 w-4 text-[#05A451]" /> Result
        </div>
        <span
          className={`rounded border px-2 py-0.5 text-xs ${reliabilityClass(
            session.reliability,
          )}`}
        >
          {session.reliability}
        </span>
      </div>

      <div className="space-y-4 p-4">
        {session.hypotheses.length > 0 && (
          <div>
            <p className="mb-1 text-xs uppercase tracking-wide text-neutral-500">
              Identified as
            </p>
            <p className="text-sm">
              {session.hypotheses[0].label}{" "}
              <span className="text-neutral-500">
                ({(session.hypotheses[0].confidence * 100).toFixed(0)}% ·{" "}
                {session.hypotheses[0].source})
              </span>
            </p>
          </div>
        )}

        {measurements.length > 0 && (
          <div>
            <p className="mb-2 flex items-center gap-1.5 text-xs uppercase tracking-wide text-neutral-500">
              <Ruler className="h-3.5 w-3.5" /> Measured
            </p>
            <div className="grid grid-cols-3 gap-2">
              {measurements.map((m) => (
                <div
                  key={m.name}
                  className={`rounded-lg border p-2 ${reliabilityClass(m.reliability)}`}
                >
                  <div className="text-[10px] uppercase opacity-70">{m.name}</div>
                  <div className="font-mono text-sm">{formatMeasurement(m)}</div>
                  <div className="mt-0.5 text-[10px] opacity-60">{m.reliability}</div>
                </div>
              ))}
            </div>
          </div>
        )}

        {session.artifacts.length > 0 && (
          <div>
            <p className="mb-2 text-xs uppercase tracking-wide text-neutral-500">
              Ready to print
            </p>
            <div className="flex flex-wrap gap-2">
              {session.artifacts.map((artifact) => (
                <a
                  key={artifact.kind}
                  href={scanApi.artifactUrl(session.session_id, artifact.kind)}
                  className="flex items-center gap-2 rounded-lg border border-neutral-700 px-3 py-2 text-xs hover:bg-neutral-800"
                >
                  <Download className="h-3.5 w-3.5" />
                  {artifact.kind}
                  <span className="text-neutral-500">
                    {(artifact.bytes / 1024).toFixed(0)} kB
                  </span>
                </a>
              ))}
            </div>
          </div>
        )}

        {session.caveats.length > 0 && (
          <div className="rounded-lg border border-amber-500/30 bg-amber-500/5 p-3">
            <p className="mb-1 text-xs font-medium text-amber-300">Caveats</p>
            <ul className="space-y-1 text-xs text-amber-200/80">
              {session.caveats.map((caveat) => (
                <li key={caveat}>— {caveat}</li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </section>
  );
}

function NumberField({
  label,
  hint,
  value,
  suffix,
  onChange,
}: {
  label: string;
  hint?: string;
  value: number;
  suffix?: string;
  onChange: (value: number) => void;
}) {
  return (
    <div className="mb-3">
      <label className="mb-1 block text-xs text-neutral-400">{label}</label>
      <div className="flex items-center gap-2">
        <input
          type="number"
          value={value}
          onChange={(e) => onChange(Number(e.target.value))}
          className="w-full rounded-lg border border-neutral-800 bg-neutral-950 px-3 py-1.5 text-sm focus:border-[#05A451] focus:outline-none"
        />
        {suffix && <span className="text-xs text-neutral-500">{suffix}</span>}
      </div>
      {hint && <p className="mt-1 text-[11px] leading-snug text-neutral-600">{hint}</p>}
    </div>
  );
}
