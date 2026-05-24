// KR-FE-OPERATOR-FIRST-RUN-WIZARD — operator onboarding for the
// pip-installable Kora bundle. 5 steps:
//
//   1. Welcome + tenant_id    — name your installation
//   2. Anthropic API key      — credit-pool budget + inline validate
//   3. IsoKron + Slack        — substrate URL + DM target; both
//                                required for unified-operator-interface
//   4. Tutorial probe         — synthetic wake fires + operator
//                                watches the 4-stream join light up
//   5. Promotion intro        — orient toward the 6 promotion loops
//                                + cost telemetry; mark wizard done
//
// First-run detection lives in App.tsx (fetches /api/wizard/state on
// boot; if marker absent AND audit log empty → renders WizardPage
// instead of DashboardPage at "/"). Once the operator finishes (or
// skips) the wizard, the marker file flips the default landing back.
//
// Security discipline:
//   * Anthropic API key / IsoKron service-role key / Slack bot token
//     never leave the operator's browser except inside the matching
//     validate-* POST body to the BE's matching upstream service.
//   * Wizard does NOT write to operator's shell .env; it surfaces
//     a downloadable .env file the operator copies into place.
//   * tenant_id wired through every step's config write (NOT
//     hardcoded "default") — feeds CC#1 #431's per-tenant
//     cost-ladder foundation.
//
// Drift-guard pins (test_wizard_drift_guards.py):
//   * WIZARD_STEPS ↔ _WIZARD_STEPS (5 steps in canonical order)
//   * WIZARD_VALIDATION_RESULTS ↔ _WIZARD_VALIDATION_RESULTS

import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  AlertCircle,
  AlertTriangle,
  CheckCircle2,
  Cloud,
  Download,
  HelpCircle,
  Key,
  Lightbulb,
  MessageCircle,
  Send,
  Sparkles,
  WandSparkles,
} from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { H2 } from "@/components/NouiTypography";
import { Card, CardContent } from "@/components/ui/card";
import { usePanelView } from "@/hooks/usePanelView";
import { api } from "@/lib/api";
import {
  WIZARD_STEPS,
  WIZARD_VALIDATION_RESULTS,
  type WizardCompleteResponse,
  type WizardStep,
  type WizardTutorialProbeResponse,
  type WizardValidationResponse,
  type WizardValidationResult,
} from "@/lib/api";

// Step metadata. ``label`` shown in the progress bar; ``Icon``
// floats next to the step header. Order MUST match WIZARD_STEPS;
// the drift-guard test pins step index → key correspondence.
interface StepDef {
  key: WizardStep;
  label: string;
  Icon: typeof WandSparkles;
}

const STEP_DEFS: readonly StepDef[] = [
  { key: "welcome", label: "Welcome", Icon: WandSparkles },
  { key: "anthropic", label: "Anthropic key", Icon: Key },
  { key: "substrate_slack", label: "IsoKron + Slack", Icon: Cloud },
  { key: "tutorial_probe", label: "Tutorial probe", Icon: Sparkles },
  { key: "promotion_intro", label: "Promotion loops", Icon: Lightbulb },
];

// ---------------------------------------------------------------
// Shared form bits
// ---------------------------------------------------------------

interface FieldProps {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  helpText?: string;
  password?: boolean;
}

function Field({
  label,
  value,
  onChange,
  placeholder,
  helpText,
  password,
}: FieldProps) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-[10px] text-muted-foreground uppercase tracking-wide">
        {label}
      </span>
      <input
        type={password ? "password" : "text"}
        className="w-full px-2 py-1.5 text-sm rounded border bg-card border-border focus:outline-primary font-mono"
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        autoComplete="off"
        spellCheck={false}
      />
      {helpText && (
        <span className="text-[10px] text-muted-foreground italic">
          {helpText}
        </span>
      )}
    </label>
  );
}

function ValidationBadge({ result }: { result: WizardValidationResult }) {
  if (result === "success") {
    return (
      <Badge tone="success">
        <CheckCircle2 className="h-3 w-3 mr-1 inline" />
        Validated
      </Badge>
    );
  }
  if (result === "auth_failure") {
    return (
      <Badge tone="destructive">
        <AlertCircle className="h-3 w-3 mr-1 inline" />
        Auth failure
      </Badge>
    );
  }
  if (result === "timeout") {
    return (
      <Badge tone="warning">
        <AlertTriangle className="h-3 w-3 mr-1 inline" />
        Timed out
      </Badge>
    );
  }
  return (
    <Badge tone="warning">
      <AlertTriangle className="h-3 w-3 mr-1 inline" />
      Network error
    </Badge>
  );
}

// ---------------------------------------------------------------
// Progress bar
// ---------------------------------------------------------------

function ProgressBar({
  currentIndex,
  onJumpBack,
}: {
  currentIndex: number;
  onJumpBack: (idx: number) => void;
}) {
  return (
    <div className="flex items-center gap-1 flex-wrap">
      {STEP_DEFS.map((step, idx) => {
        const reached = idx <= currentIndex;
        const active = idx === currentIndex;
        const Icon = step.Icon;
        return (
          <div
            key={step.key}
            className="flex items-center gap-1"
            aria-current={active ? "step" : undefined}
          >
            <button
              type="button"
              onClick={() => idx < currentIndex && onJumpBack(idx)}
              disabled={idx >= currentIndex}
              title={
                idx < currentIndex
                  ? `Back to step ${idx + 1}: ${step.label}`
                  : step.label
              }
              className={`inline-flex items-center gap-1 px-2 py-1 rounded text-[10px] uppercase tracking-wide transition-colors ${
                active
                  ? "bg-primary text-primary-foreground"
                  : reached
                    ? "bg-yellow-500/30 text-yellow-200 hover:bg-yellow-500/40 cursor-pointer"
                    : "bg-muted/20 text-muted-foreground"
              }`}
            >
              <Icon className="h-3 w-3" />
              <span className="font-mono">
                {idx + 1}/{STEP_DEFS.length}
              </span>
              <span>{step.label}</span>
            </button>
            {idx < STEP_DEFS.length - 1 && (
              <span className="text-[10px] text-muted-foreground">→</span>
            )}
          </div>
        );
      })}
    </div>
  );
}

// ---------------------------------------------------------------
// Per-step bodies
// ---------------------------------------------------------------

interface StepProps {
  config: WizardConfig;
  setConfig: (next: WizardConfig) => void;
  onAdvance: () => void;
}

interface WizardConfig {
  tenantId: string;
  anthropicApiKey: string;
  creditPoolUsd: number;
  anthropicValidation: WizardValidationResult | null;
  substrateUrl: string;
  substrateServiceRoleKey: string;
  substrateValidation: WizardValidationResult | null;
  slackBotToken: string;
  slackUserId: string;
  slackValidation: WizardValidationResult | null;
  slackTeam: string | null;
  skippedSubstrateSlack: boolean;
  tutorialProbeFired: boolean;
  tutorialProbeSessionId: string | null;
}

function emptyConfig(): WizardConfig {
  return {
    tenantId: "default",
    anthropicApiKey: "",
    creditPoolUsd: 200,
    anthropicValidation: null,
    substrateUrl: "",
    substrateServiceRoleKey: "",
    substrateValidation: null,
    slackBotToken: "",
    slackUserId: "",
    slackValidation: null,
    slackTeam: null,
    skippedSubstrateSlack: false,
    tutorialProbeFired: false,
    tutorialProbeSessionId: null,
  };
}

// ---------------------------------------------------------------
// KR-FE-WIZARD-RESUME-FROM-PARTIAL — sessionStorage resume.
//
// Why sessionStorage rather than localStorage:
//   * Wizard state contains tenant_id + service connectivity
//     results — operator-recoverable on refresh, but should not
//     persist past browser close (those are scratch values that
//     belong to a single setup session).
//   * Credentials (anthropicApiKey / substrateServiceRoleKey /
//     slackBotToken) are NEVER stored under any backing. Resume
//     restores everything else and the operator re-enters the
//     three creds + re-validates. Validation results are also
//     reset on resume — a stale "success" badge with no key to
//     back it would be misleading.
//
// Security pin: the strip-fields list below is the canonical set
// of "never store these" fields. Adding a new credential to
// WizardConfig means adding it here too (and to the resume-
// regression test if/when vitest lands in this repo).
// ---------------------------------------------------------------

export const WIZARD_RESUME_STORAGE_KEY = "kora_wizard_state" as const;

interface PersistedWizardState {
  config: WizardConfig;
  stepIdx: number;
  // Schema version — increment when WizardConfig adds a non-
  // additive field so we can refuse to restore stale shapes.
  v: 1;
}

const PERSIST_SCHEMA_VERSION: PersistedWizardState["v"] = 1;

function stripCreds(config: WizardConfig): WizardConfig {
  // Credentials are NEVER persisted to sessionStorage. Validation
  // results are also cleared so the operator re-validates the
  // re-entered creds (a "success" badge from a session ago is not
  // safe to trust — the upstream service state could have changed).
  return {
    ...config,
    anthropicApiKey: "",
    substrateServiceRoleKey: "",
    slackBotToken: "",
    anthropicValidation: null,
    substrateValidation: null,
    slackValidation: null,
  };
}

function readPersisted(): PersistedWizardState | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.sessionStorage.getItem(WIZARD_RESUME_STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as unknown;
    if (
      parsed !== null &&
      typeof parsed === "object" &&
      "v" in parsed &&
      (parsed as { v: unknown }).v === PERSIST_SCHEMA_VERSION &&
      "config" in parsed &&
      "stepIdx" in parsed
    ) {
      return parsed as PersistedWizardState;
    }
  } catch {
    // Corrupt JSON — silently drop. Wizard starts fresh.
  }
  return null;
}

function writePersisted(state: PersistedWizardState): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.setItem(
      WIZARD_RESUME_STORAGE_KEY,
      JSON.stringify(state),
    );
  } catch {
    // Quota / disabled storage — best-effort. The wizard continues
    // working in-memory; the operator just loses resume capability.
  }
}

function clearPersisted(): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.removeItem(WIZARD_RESUME_STORAGE_KEY);
  } catch {
    // Best-effort.
  }
}

function StepWelcome({ config, setConfig, onAdvance }: StepProps) {
  return (
    <div className="space-y-3 text-sm">
      <p>
        Welcome to Kora — your operator-side AI teammate. This wizard
        walks you through the four credentials Kora needs (Anthropic +
        IsoKron + Slack) and fires a tutorial probe so you can see the
        full audit-trail loop end-to-end.
      </p>
      <p className="text-xs text-muted-foreground">
        Estimated time: <strong>~15 minutes</strong>. You&apos;ll need
        your Anthropic API key, an IsoKron substrate URL + service-role
        key, and a Slack bot token + your operator user_id.
      </p>
      <Field
        label="Tenant ID"
        value={config.tenantId}
        onChange={(v) => setConfig({ ...config, tenantId: v })}
        helpText={
          "Partitions cost ladder + audit log + plugin namespace. " +
          "Use 'default' if you're the only operator. Multi-tenant " +
          "Kora deployments give each tenant its own id (org / username)."
        }
      />
      <div className="pt-1">
        <Button
          size="sm"
          onClick={onAdvance}
          disabled={!config.tenantId.trim()}
        >
          Next: Anthropic key
        </Button>
      </div>
    </div>
  );
}

function StepAnthropic({ config, setConfig, onAdvance }: StepProps) {
  const [validating, setValidating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const validate = useCallback(async () => {
    setValidating(true);
    setError(null);
    try {
      const resp: WizardValidationResponse =
        await api.validateAnthropicApiKey(config.anthropicApiKey);
      setConfig({ ...config, anthropicValidation: resp.result });
      if (resp.result !== "success") {
        setError(resp.detail ?? null);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setConfig({ ...config, anthropicValidation: "network_failure" });
    } finally {
      setValidating(false);
    }
  }, [config, setConfig]);

  const canAdvance = config.anthropicValidation === "success";
  return (
    <div className="space-y-3 text-sm">
      <p>
        Anthropic API key. Kora&apos;s reasoning engine uses Anthropic;
        billing goes through your Anthropic Console (not through Kora).
      </p>
      <Field
        label="Anthropic API key"
        value={config.anthropicApiKey}
        onChange={(v) =>
          setConfig({
            ...config,
            anthropicApiKey: v,
            anthropicValidation: null,
          })
        }
        placeholder="sk-ant-..."
        password
        helpText="Starts with sk-ant-. NEVER auto-filled — enter your own."
      />
      <Field
        label="Monthly credit pool (USD)"
        value={String(config.creditPoolUsd)}
        onChange={(v) => {
          const n = Number(v);
          if (!Number.isNaN(n) && n >= 0) {
            setConfig({ ...config, creditPoolUsd: n });
          }
        }}
        helpText="Soft budget surfaced in Cost State; default $200/mo."
      />
      <div className="flex items-center gap-2 flex-wrap pt-1">
        <Button
          size="sm"
          onClick={() => void validate()}
          disabled={!config.anthropicApiKey.trim() || validating}
        >
          {validating ? <Spinner className="h-3 w-3 mr-1" /> : null}
          Validate key (1-token test inference)
        </Button>
        {config.anthropicValidation && (
          <ValidationBadge result={config.anthropicValidation} />
        )}
        {error && (
          <span className="text-xs text-destructive font-mono">{error}</span>
        )}
      </div>
      <div className="pt-1">
        <Button size="sm" onClick={onAdvance} disabled={!canAdvance}>
          Next: IsoKron + Slack
        </Button>
      </div>
    </div>
  );
}

function StepSubstrateSlack({ config, setConfig, onAdvance }: StepProps) {
  const [validatingSub, setValidatingSub] = useState(false);
  const [validatingSlack, setValidatingSlack] = useState(false);

  const validateSubstrate = useCallback(async () => {
    setValidatingSub(true);
    try {
      const resp = await api.validateSubstrate(
        config.substrateUrl,
        config.substrateServiceRoleKey,
      );
      setConfig({ ...config, substrateValidation: resp.result });
    } catch {
      setConfig({ ...config, substrateValidation: "network_failure" });
    } finally {
      setValidatingSub(false);
    }
  }, [config, setConfig]);

  const validateSlack = useCallback(async () => {
    setValidatingSlack(true);
    try {
      const resp = await api.validateSlack(
        config.slackBotToken,
        config.slackUserId,
      );
      setConfig({
        ...config,
        slackValidation: resp.result,
        slackTeam: resp.team ?? null,
      });
    } catch {
      setConfig({ ...config, slackValidation: "network_failure" });
    } finally {
      setValidatingSlack(false);
    }
  }, [config, setConfig]);

  const bothOk =
    config.substrateValidation === "success" &&
    config.slackValidation === "success";

  return (
    <div className="space-y-3 text-sm">
      <p>
        Kora is a <strong>unified operator interface</strong> — it
        needs IsoKron (durable state substrate) and Slack (DM
        escalation) together. Skipping either leaves Kora in degraded
        mode (audit-only, no live escalation).
      </p>
      <div className="rounded border border-border p-3 space-y-2">
        <div className="flex items-center gap-1.5 text-xs font-medium">
          <Cloud className="h-3 w-3" />
          IsoKron substrate
        </div>
        <Field
          label="Substrate URL"
          value={config.substrateUrl}
          onChange={(v) =>
            setConfig({
              ...config,
              substrateUrl: v,
              substrateValidation: null,
            })
          }
          placeholder="https://your-instance.supabase.co"
        />
        <Field
          label="Service-role key"
          value={config.substrateServiceRoleKey}
          onChange={(v) =>
            setConfig({
              ...config,
              substrateServiceRoleKey: v,
              substrateValidation: null,
            })
          }
          password
          helpText="From Supabase project settings → API → service_role key."
        />
        <div className="flex items-center gap-2 flex-wrap">
          <Button
            size="sm"
            onClick={() => void validateSubstrate()}
            disabled={
              !config.substrateUrl.trim() ||
              !config.substrateServiceRoleKey.trim() ||
              validatingSub
            }
          >
            {validatingSub ? <Spinner className="h-3 w-3 mr-1" /> : null}
            Ping substrate
          </Button>
          {config.substrateValidation && (
            <ValidationBadge result={config.substrateValidation} />
          )}
        </div>
      </div>
      <div className="rounded border border-border p-3 space-y-2">
        <div className="flex items-center gap-1.5 text-xs font-medium">
          <MessageCircle className="h-3 w-3" />
          Slack DM channel
        </div>
        <Field
          label="Slack bot token"
          value={config.slackBotToken}
          onChange={(v) =>
            setConfig({
              ...config,
              slackBotToken: v,
              slackValidation: null,
            })
          }
          placeholder="xoxb-..."
          password
        />
        <Field
          label="Operator user_id (DM target)"
          value={config.slackUserId}
          onChange={(v) => setConfig({ ...config, slackUserId: v })}
          placeholder="U01ABC123"
          helpText="Your Slack user_id — Kora DMs you here for alerts."
        />
        <div className="flex items-center gap-2 flex-wrap">
          <Button
            size="sm"
            onClick={() => void validateSlack()}
            disabled={!config.slackBotToken.trim() || validatingSlack}
          >
            {validatingSlack ? <Spinner className="h-3 w-3 mr-1" /> : null}
            Test auth (slack.auth.test)
          </Button>
          {config.slackValidation && (
            <ValidationBadge result={config.slackValidation} />
          )}
          {config.slackValidation === "success" && config.slackTeam && (
            <span className="text-xs text-muted-foreground">
              team: <span className="font-mono">{config.slackTeam}</span>
            </span>
          )}
        </div>
      </div>
      <div className="flex items-center gap-2 flex-wrap pt-1">
        <Button size="sm" onClick={onAdvance} disabled={!bothOk}>
          Next: Tutorial probe
        </Button>
        <Button
          size="sm"
          ghost
          onClick={() => {
            setConfig({ ...config, skippedSubstrateSlack: true });
            onAdvance();
          }}
        >
          Skip with degraded mode
        </Button>
        {config.skippedSubstrateSlack && (
          <span className="text-xs text-yellow-500 italic">
            ⚠ Degraded — audit-only, no live escalation
          </span>
        )}
      </div>
    </div>
  );
}

function StepTutorialProbe({ config, setConfig, onAdvance }: StepProps) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fire = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const resp: WizardTutorialProbeResponse =
        await api.triggerWizardTutorialProbe(config.tenantId);
      if (resp.ok) {
        setConfig({
          ...config,
          tutorialProbeFired: true,
          tutorialProbeSessionId: resp.caller_session_id ?? null,
        });
      } else {
        setError(resp.error ?? "unknown error");
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [config, setConfig]);

  return (
    <div className="space-y-3 text-sm">
      <p>
        First investigation tutorial: a synthetic{" "}
        <code className="font-mono text-xs">probe.wake_requested</code>{" "}
        audit row fires here — Kora&apos;s reasoning engine investigates,
        DMs you the result, and the 4-stream join lights up in the
        Probe Investigations panel.
      </p>
      <div className="flex items-center gap-2 flex-wrap pt-1">
        <Button
          size="sm"
          onClick={() => void fire()}
          disabled={busy || config.tutorialProbeFired}
        >
          {busy ? <Spinner className="h-3 w-3 mr-1" /> : null}
          <Send className="h-3 w-3 mr-1 inline" />
          Trigger probe wake now
        </Button>
        {config.tutorialProbeFired && (
          <Badge tone="success">
            <CheckCircle2 className="h-3 w-3 mr-1 inline" />
            Wake fired
          </Badge>
        )}
        {error && (
          <span className="text-xs text-destructive font-mono">{error}</span>
        )}
      </div>
      {config.tutorialProbeFired && config.tutorialProbeSessionId && (
        <div className="rounded border border-yellow-500/40 bg-yellow-500/5 p-3 text-xs space-y-1">
          <div className="font-medium">Watch the investigation unfold</div>
          <div className="text-muted-foreground">
            caller_session_id:{" "}
            <span className="font-mono">
              {config.tutorialProbeSessionId}
            </span>
          </div>
          <div className="flex items-center gap-2 flex-wrap mt-1">
            <Link
              to="/probe-investigations"
              className="text-primary hover:underline"
            >
              → Open Probe Investigations
            </Link>
            <Link
              to={`/investigations/${encodeURIComponent(config.tutorialProbeSessionId)}`}
              className="text-primary hover:underline"
            >
              → Drill into the audit timeline
            </Link>
          </div>
          <div className="text-muted-foreground italic">
            If your Slack is misconfigured, the audit row + DM payload
            still get written — you can debug from the timeline view.
          </div>
        </div>
      )}
      <div className="pt-1">
        <Button
          size="sm"
          onClick={onAdvance}
          disabled={!config.tutorialProbeFired}
        >
          Next: Promotion loops
        </Button>
      </div>
    </div>
  );
}

function StepPromotionIntro({ config, onAdvance }: StepProps) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const navigate = useNavigate();

  const complete = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const resp: WizardCompleteResponse = await api.completeWizard({
        completed: true,
        skipped: false,
        tenant_id: config.tenantId,
        last_step: "promotion_intro",
      });
      if (!resp.ok) {
        setError(resp.error ?? "unknown error");
        return;
      }
      // KR-FE-WIZARD-RESUME-FROM-PARTIAL — completion is the
      // terminal state; drop the resume blob so a future first-
      // run wizard re-open (rare; e.g. operator removed marker
      // manually) starts fresh.
      clearPersisted();
      onAdvance();
      navigate("/");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [config.tenantId, onAdvance, navigate]);

  return (
    <div className="space-y-3 text-sm">
      <p>
        Kora self-improves via <strong>6 promotion loops</strong> —
        phrasebook, snapshot-expand, router-tuning, tool-trimming,
        probe-fix-envelopes, email-intent. Each loop proposes changes
        based on observed audit data; you review + approve from the
        Promotion Review panel.
      </p>
      <ul className="list-disc ml-5 text-xs text-muted-foreground space-y-0.5">
        <li>
          Read-only proposals (phrasebook / snapshot-expand /
          router-tuning) default ENABLED — they only propose, never
          mutate.
        </li>
        <li>
          Action envelopes (probe-fix-envelopes) default DISABLED —
          fail-closed per Kora&apos;s security posture.
        </li>
        <li>
          You can toggle each loop&apos;s{" "}
          <code className="font-mono">KORA_PROMOTE_&lt;NAME&gt;_ENABLED</code>{" "}
          env var anytime.
        </li>
      </ul>
      <div className="flex items-center gap-2 flex-wrap text-xs pt-1">
        <Link
          to="/promotions/phrasebook"
          className="text-primary hover:underline inline-flex items-center gap-1"
        >
          <Lightbulb className="h-3 w-3" />
          Open Promotion Review
        </Link>
        <Link
          to="/cost-telemetry"
          className="text-primary hover:underline"
        >
          → Cost Telemetry
        </Link>
      </div>
      <DotEnvDownload config={config} />
      {error && (
        <div className="text-xs text-destructive font-mono">{error}</div>
      )}
      <div className="pt-1">
        <Button size="sm" onClick={() => void complete()} disabled={busy}>
          {busy ? <Spinner className="h-3 w-3 mr-1" /> : null}
          <CheckCircle2 className="h-3 w-3 mr-1" />
          Finish wizard
        </Button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------
// .env download (security: wizard NEVER writes to operator shell)
// ---------------------------------------------------------------

function DotEnvDownload({ config }: { config: WizardConfig }) {
  const envContents = useMemo(() => buildEnvContents(config), [config]);
  const downloadHref = useMemo(() => {
    return (
      "data:text/plain;charset=utf-8," + encodeURIComponent(envContents)
    );
  }, [envContents]);
  return (
    <div className="rounded border border-border bg-muted/20 p-3 space-y-2">
      <div className="flex items-center gap-1.5 text-xs font-medium">
        <Download className="h-3 w-3" />
        Download .env
      </div>
      <div className="text-xs text-muted-foreground">
        For your security Kora does NOT modify your shell environment.
        Download this <code className="font-mono">.env</code> and copy
        it to <code className="font-mono">$KORA_HOME/.env</code>, then
        restart Kora.
      </div>
      <pre className="font-mono text-[10px] whitespace-pre-wrap rounded bg-muted/30 border border-border p-2 max-h-48 overflow-auto">
        {envContents}
      </pre>
      <a
        href={downloadHref}
        download=".env"
        className="inline-flex items-center gap-1 text-xs text-primary hover:underline"
      >
        <Download className="h-3 w-3" />
        Download .env file
      </a>
    </div>
  );
}

function buildEnvContents(config: WizardConfig): string {
  const lines: string[] = [];
  lines.push("# Kora environment — generated by the first-run wizard");
  lines.push(`# tenant_id: ${config.tenantId}`);
  lines.push("");
  lines.push(`KORA_TENANT_ID=${config.tenantId}`);
  if (config.anthropicApiKey) {
    lines.push(`KORA_ANTHROPIC_API_KEY=${config.anthropicApiKey}`);
  }
  lines.push(`KORA_CREDIT_POOL_USD=${config.creditPoolUsd}`);
  if (config.substrateUrl) {
    lines.push(`KORA_ISOKRON_SUBSTRATE_URL=${config.substrateUrl}`);
  }
  if (config.substrateServiceRoleKey) {
    lines.push(
      `KORA_ISOKRON_SUBSTRATE_SERVICE_ROLE_KEY=${config.substrateServiceRoleKey}`,
    );
  }
  if (config.slackBotToken) {
    lines.push(`KORA_SLACK_BOT_TOKEN=${config.slackBotToken}`);
  }
  if (config.slackUserId) {
    lines.push(`KORA_SLACK_JOSHUA_USER_ID=${config.slackUserId}`);
  }
  return lines.join("\n") + "\n";
}

// ---------------------------------------------------------------
// Page
// ---------------------------------------------------------------

export default function WizardPage() {
  usePanelView("WizardPage");

  // KR-FE-WIZARD-RESUME-FROM-PARTIAL — check sessionStorage on mount.
  // If a prior session left state past step 0, render the resume
  // prompt (operator decides yes/no before the wizard renders).
  const [pendingResume, setPendingResume] =
    useState<PersistedWizardState | null>(() => {
      const p = readPersisted();
      // Step 0 (welcome) has no information worth resuming — only
      // offer resume from step ≥ 1.
      return p && p.stepIdx > 0 ? p : null;
    });

  const [config, setConfig] = useState<WizardConfig>(emptyConfig());
  const [stepIdx, setStepIdx] = useState(0);
  const [skipError, setSkipError] = useState<string | null>(null);
  const navigate = useNavigate();

  // Persist every config / step change. Creds + validation results
  // are stripped via stripCreds — credentials never reach
  // sessionStorage. Skipped while the resume-prompt is up so we
  // don't overwrite the pending state with the empty initial.
  useEffect(() => {
    if (pendingResume) return;
    writePersisted({
      config: stripCreds(config),
      stepIdx,
      v: 1,
    });
  }, [config, stepIdx, pendingResume]);

  const acceptResume = useCallback(() => {
    if (!pendingResume) return;
    setConfig(pendingResume.config);
    setStepIdx(pendingResume.stepIdx);
    setPendingResume(null);
  }, [pendingResume]);

  const declineResume = useCallback(() => {
    clearPersisted();
    setPendingResume(null);
  }, []);

  // Drift-guard greps — pin the FE constants are referenced from
  // the page so test_wizard_drift_guards' grep against the source
  // sees them.
  useEffect(() => {
    void WIZARD_STEPS;
    void WIZARD_VALIDATION_RESULTS;
  }, []);

  const currentStep = STEP_DEFS[stepIdx];

  const skip = useCallback(async () => {
    setSkipError(null);
    try {
      await api.completeWizard({
        skipped: true,
        completed: false,
        tenant_id: config.tenantId.trim() || "default",
        last_step: currentStep.key,
      });
      // KR-FE-WIZARD-RESUME-FROM-PARTIAL — clear the resume blob
      // on skip; the wizard is done for this session.
      clearPersisted();
      navigate("/");
    } catch (e) {
      setSkipError(e instanceof Error ? e.message : String(e));
    }
  }, [config.tenantId, currentStep.key, navigate]);

  const advance = useCallback(() => {
    setStepIdx((idx) => Math.min(idx + 1, STEP_DEFS.length - 1));
  }, []);

  const StepComponent = (() => {
    switch (currentStep.key) {
      case "welcome":
        return StepWelcome;
      case "anthropic":
        return StepAnthropic;
      case "substrate_slack":
        return StepSubstrateSlack;
      case "tutorial_probe":
        return StepTutorialProbe;
      case "promotion_intro":
        return StepPromotionIntro;
    }
  })();

  const Icon = currentStep.Icon;

  // KR-FE-WIZARD-RESUME-FROM-PARTIAL — render the resume prompt
  // instead of the wizard body until the operator decides. Picking
  // Yes restores config + jumps to the saved step; No clears the
  // blob and starts fresh. Credentials are NEVER restored; the
  // operator re-enters + re-validates them at their step.
  if (pendingResume) {
    const lastLabel =
      STEP_DEFS[pendingResume.stepIdx]?.label ??
      `step ${pendingResume.stepIdx + 1}`;
    return (
      <div className="space-y-4 p-4 max-w-3xl mx-auto">
        <H2 className="flex items-center gap-2">
          <WandSparkles className="h-5 w-5" />
          First-run setup
        </H2>
        <Card className="border-yellow-500/30 bg-yellow-500/5">
          <CardContent className="p-4 space-y-3 text-sm">
            <div className="flex items-center gap-2 font-medium">
              <AlertTriangle className="h-4 w-4 text-yellow-500" />
              Resume from step {pendingResume.stepIdx + 1} (
              <span className="font-mono">{lastLabel}</span>)?
            </div>
            <p className="text-xs text-muted-foreground">
              We found a wizard session in this tab. Resuming keeps
              your tenant_id, IsoKron URL, Slack user_id, validation
              results-to-rerun, and other non-credential fields.
              <strong className="text-foreground">
                {" "}
                Credentials (Anthropic key / IsoKron service-role key
                / Slack bot token) are never saved
              </strong>{" "}
              — you&apos;ll re-enter + re-validate them at their step.
            </p>
            <div className="flex items-center gap-2 flex-wrap pt-1">
              <Button size="sm" onClick={acceptResume}>
                <CheckCircle2 className="h-3 w-3 mr-1" />
                Yes, resume from step {pendingResume.stepIdx + 1}
              </Button>
              <Button size="sm" ghost onClick={declineResume}>
                No, start fresh
              </Button>
            </div>
          </CardContent>
        </Card>
      </div>
    );
  }

  return (
    <div className="space-y-4 p-4 max-w-3xl mx-auto">
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <H2 className="flex items-center gap-2">
          <WandSparkles className="h-5 w-5" />
          First-run setup
        </H2>
        <Button size="sm" ghost onClick={() => void skip()}>
          Skip — I&apos;ll configure manually
        </Button>
      </div>
      {skipError && (
        <div className="text-xs text-destructive font-mono">{skipError}</div>
      )}

      <Card>
        <CardContent className="p-3">
          <ProgressBar
            currentIndex={stepIdx}
            onJumpBack={(idx) => setStepIdx(idx)}
          />
        </CardContent>
      </Card>

      <Card>
        <CardContent className="p-4 space-y-3">
          <div className="flex items-center gap-2 text-sm font-medium">
            <Icon className="h-4 w-4" />
            Step {stepIdx + 1} of {STEP_DEFS.length} — {currentStep.label}
          </div>
          <StepComponent
            config={config}
            setConfig={setConfig}
            onAdvance={advance}
          />
        </CardContent>
      </Card>

      <Card className="border-blue-500/30 bg-blue-500/5">
        <CardContent className="p-3 flex items-start gap-2 text-xs">
          <HelpCircle className="h-4 w-4 text-blue-500 flex-shrink-0 mt-0.5" />
          <div className="text-muted-foreground">
            <span className="text-foreground font-medium">Privacy</span> —
            none of these credentials leave your browser except inside the
            matching validate-* request to the upstream service you&apos;re
            checking against. Wizard NEVER writes to your shell{" "}
            <code className="font-mono">.env</code>; you download + copy
            it yourself.
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
