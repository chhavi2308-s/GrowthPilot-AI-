"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { useAuth, useCan } from "@/components/auth";
import { ConnectionBadge, EvidenceBadge, PlatformChip, SourceLink, SyntheticBadge, ValidationPanel } from "@/components/domain";
import { PlatformPreview } from "@/components/post-preview";
import { Alert, Badge, Button, Card, Empty, Loading, PageHeader, Textarea, useAction, useApi } from "@/components/ui";
import { api } from "@/lib/api";
import { cx, fmtDate } from "@/lib/format";
import type { ApprovalRequest, ContentItem } from "@/lib/types";

interface Inbox { items: ContentItem[]; requests: ApprovalRequest[]; live_publishing_enabled: boolean; mode: string }

const KIND: Record<ApprovalRequest["kind"], string> = {
  budget: "Budget increase", strategy_change: "Strategy change", paid_promotion: "Paid promotion", account_change: "Account change",
};

function RequestCard({ r, onDone }: { r: ApprovalRequest; onDone: () => void }) {
  const can = useCan();
  const [note, setNote] = useState("");
  const act = useAction();
  const decide = async (approve: boolean) => {
    if (await act.run(() => api.post(`/api/approval-requests/${r.id}/decide`, { approve, note }))) onDone();
  };
  return (
    <Card title={<span className="flex flex-wrap items-center gap-2">{r.title}<Badge tone="warn">{KIND[r.kind]}</Badge></span>}
      subtitle={`Requested ${fmtDate(r.requested_at, can.tz)}${r.run_id ? " by an agent run" : ""}`}
      actions={r.run_id && <Link href={`/agent/${r.run_id}`} className="text-sm text-accent hover:underline">View run →</Link>}>
      <p className="text-sm">{r.detail}</p>
      {r.kind === "budget" && <p className="mt-1 text-xs text-muted">Needed ≈ ${r.payload.needed_usd?.toFixed(4)} · scope {r.payload.scope} · spent ${r.payload.spent_usd?.toFixed(4) ?? "0"} of ${r.payload.budget_usd?.toFixed(2) ?? "?"} · blocked tool {r.payload.tool}</p>}
      {can.own ? (
        <div className="mt-3 space-y-2">
          <Textarea aria-label="Note (optional)" rows={2} placeholder="Note (optional)" value={note} onChange={(e) => setNote(e.target.value)} />
          {act.error && <Alert tone="bad">{act.error}</Alert>}
          <div className="flex gap-2"><Button variant="ok" busy={act.busy} onClick={() => decide(true)}>Approve</Button><Button variant="danger" busy={act.busy} onClick={() => decide(false)}>Reject</Button></div>
        </div>
      ) : <div className="mt-2"><Alert tone="info">Only a workspace owner can decide.</Alert></div>}
    </Card>
  );
}

function VersionPanel({ item, mode, liveEnabled, onDone }: { item: ContentItem; mode: string; liveEnabled: boolean; onDone: () => void }) {
  const can = useCan();
  const { state } = useAuth();
  const [ack, setAck] = useState(false);
  const [authorize, setAuthorize] = useState(false);
  const [note, setNote] = useState("");
  const [showChanges, setShowChanges] = useState(false);
  const act = useAction();
  const v = item.validation;
  const warnings = v ? [...v.product_claims, ...v.brand, ...v.platform].filter((c) => c.status === "warn") : [];
  const failures = v?.failures ?? 0;
  const hasLiveTarget = (item.target_connections || []).some((c) => c.adapter_info?.external);
  const approve = async () => {
    if (await act.run(() => api.post(`/api/content/${item.id}/approve`, { version: item.version, acknowledge_warnings: ack, authorize_publish: authorize }))) onDone();
  };
  const changes = async () => {
    if (await act.run(() => api.post(`/api/content/${item.id}/request-changes`, { note }))) onDone();
  };
  return (
    <div className="grid gap-5 lg:grid-cols-[minmax(0,340px)_minmax(0,1fr)]">
      <PlatformPreview platform={item.platform} assets={item.assets || []} caption={item.final_caption || ""} account={item.brand_name || state?.workspace?.name?.replace(/ \(demo\)$/, "") || "brand"} type={item.content_type} title={item.hook} limit={item.platform_spec?.caption_max} />
      <div className="min-w-0 space-y-3">
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <PlatformChip platform={item.platform} withLabel /><Badge>v{item.version}</Badge><Badge>{item.content_type}</Badge>
          <Link href={`/content/${item.id}`} className="text-accent hover:underline">Open in Creative Studio →</Link>
        </div>
        <div className="grid gap-3 text-sm md:grid-cols-2">
          <div><div className="text-xs font-semibold uppercase text-muted">Intended publication</div>{fmtDate(item.planned_at, can.tz)} <span className="text-muted">({can.tz})</span></div>
          <div><div className="text-xs font-semibold uppercase text-muted">Target account</div>
            {(item.target_connections || []).length === 0 ? <span className="text-warn">No verified connection for this platform</span> :
              item.target_connections!.map((c) => <div key={c.id} className="flex items-center gap-1.5">{c.account_label || c.name}<ConnectionBadge status={c.status} /></div>)}</div>
        </div>
        <div className="rounded-lg border border-line p-3"><ValidationPanel v={v} version={item.version} /></div>
        {(item.assets || []).some((a) => a.meta?.checks) && (
          <div className="text-xs text-muted">Image checks: {(item.assets || []).filter((a) => a.meta?.ok).length}/{(item.assets || []).length} image(s) pass contrast, safe-area, text-fit and size checks. Approval covers these exact image files.</div>
        )}
        {can.own ? (
          <div className="space-y-2">
            {warnings.length > 0 && (
              <label className="flex items-start gap-2 text-sm"><input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} className="mt-1" />
                I&apos;ve reviewed the {warnings.length} warning(s) above.</label>
            )}
            {mode === "live" && (
              <label className="flex items-start gap-2 text-sm text-bad"><input type="checkbox" checked={authorize} onChange={(e) => setAuthorize(e.target.checked)} className="mt-1" />
                Also authorise publishing this exact version (v{item.version}) to the real account. {hasLiveTarget ? "" : "(No live account is connected yet; this applies once one is.) "}{liveEnabled ? "" : "(Live publishing is currently off.)"}</label>
            )}
            {act.error && <Alert tone="bad">{act.error}</Alert>}
            <div className="flex flex-wrap gap-2">
              <Button variant="ok" busy={act.busy} disabled={failures > 0 || (warnings.length > 0 && !ack)} onClick={approve}>Approve {item.platform} v{item.version}</Button>
              <Button onClick={() => setShowChanges(!showChanges)}>Request changes</Button>
            </div>
            {failures > 0 && <p className="text-xs text-bad">Can&apos;t approve: {failures} validation failure(s). Edit the post first.</p>}
            {showChanges && (
              <div className="space-y-2">
                <Textarea aria-label="What should change?" placeholder="What should change?" value={note} onChange={(e) => setNote(e.target.value)} />
                <Button variant="danger" busy={act.busy} disabled={!note.trim()} onClick={changes}>Send back</Button>
              </div>
            )}
          </div>
        ) : <Alert tone="info">Only a workspace owner can approve.</Alert>}
      </div>
    </div>
  );
}

function FamilyCard({ items, mode, liveEnabled, onDone }: { items: ContentItem[]; mode: string; liveEnabled: boolean; onDone: () => void }) {
  const can = useCan();
  const [tab, setTab] = useState(items[0].id);
  const [ackAll, setAckAll] = useState(false);
  const bulk = useAction();
  const cur = items.find((i) => i.id === tab) || items[0];
  const head = items.find((i) => !i.master_id) || items[0];
  const passing = items.filter((i) => i.validation?.ok);
  const anyWarnings = passing.some((i) => (i.validation?.warnings ?? 0) > 0);
  const approveAll = async () => {
    await bulk.run(async () => {
      for (const i of passing) await api.post(`/api/content/${i.id}/approve`, { version: i.version, acknowledge_warnings: ackAll });
    });
    onDone();
  };
  return (
    <Card>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <span className="text-lg font-semibold">{head.title}</span>
        {head.variant_label && <Badge tone="info">variant {head.variant_label}</Badge>}
        <SyntheticBadge show={head.is_synthetic} label="Demo" />
        {head.campaign && <Link href={`/campaigns/${head.campaign.id}`} className="text-sm text-accent hover:underline">{head.campaign.name}</Link>}
      </div>
      {head.hypothesis && <p className="mb-2 text-sm"><span className="text-xs font-semibold uppercase text-muted">Hypothesis: </span>{head.hypothesis.statement}</p>}
      {head.evidence && head.evidence.length > 0 && (
        <details className="mb-3 text-sm">
          <summary className="cursor-pointer text-xs font-semibold uppercase text-muted">Supporting evidence ({head.evidence.length})</summary>
          <ul className="mt-2 space-y-1.5">
            {head.evidence.map((f) => (
              <li key={f.id} className="flex flex-wrap items-center gap-1.5"><EvidenceBadge c={f.classification} /><SyntheticBadge show={f.is_synthetic} /> {f.statement.slice(0, 200)} <span className="text-xs text-muted">— <SourceLink f={f} /></span></li>
            ))}
          </ul>
        </details>
      )}
      <div role="tablist" className="mb-4 flex flex-wrap gap-1 border-b border-line">
        {items.map((i) => (
          <button key={i.id} role="tab" aria-selected={i.id === cur.id} onClick={() => setTab(i.id)}
            className={cx("-mb-px flex items-center gap-1.5 border-b-2 px-3 py-2 text-sm font-semibold", i.id === cur.id ? "border-accent text-accent" : "border-transparent text-muted hover:text-ink")}>
            <PlatformChip platform={i.platform} /> {i.platform}
            {i.validation?.ok ? <span className="text-ok">✓</span> : <span className="text-bad">✕</span>}
          </button>
        ))}
      </div>
      <VersionPanel key={`${cur.id}-${cur.version}`} item={cur} mode={mode} liveEnabled={liveEnabled} onDone={onDone} />
      {can.own && items.length > 1 && passing.length > 0 && (
        <div className="mt-4 flex flex-wrap items-center gap-3 border-t border-line pt-3 text-sm">
          {anyWarnings && <label className="flex items-center gap-2"><input type="checkbox" checked={ackAll} onChange={(e) => setAckAll(e.target.checked)} />I&apos;ve reviewed the warnings on every version</label>}
          <Button variant="ok" busy={bulk.busy} disabled={anyWarnings && !ackAll} onClick={approveAll}>Approve all {passing.length} passing version(s)</Button>
          <span className="text-xs text-muted">Each platform version is approved separately, bound to its exact text and image.</span>
          {bulk.error && <Alert tone="bad">{bulk.error}</Alert>}
        </div>
      )}
    </Card>
  );
}

export default function ApprovalsPage() {
  const { data, loading, error, reload } = useApi<Inbox>("/api/approvals");
  const families = useMemo(() => {
    const m = new Map<number, ContentItem[]>();
    for (const i of data?.items || []) {
      const key = i.master_id ?? i.id;
      m.set(key, [...(m.get(key) || []), i]);
    }
    return [...m.values()].map((g) => g.sort((a, b) => (a.master_id ? 1 : 0) - (b.master_id ? 1 : 0) || a.id - b.id));
  }, [data]);
  return (
    <div>
      <PageHeader title="Approval Inbox" subtitle="Nothing is scheduled or published until a person approves the exact version, platform variant and image shown here. Any later edit or re-render needs approval again." />
      {loading && !data ? <Loading /> : error ? <Alert tone="bad">{error}</Alert> : !data?.items.length && !data?.requests.length ? (
        <Empty title="Nothing waiting for approval">When the agent finishes a campaign it sends verified posts here, grouped by post with one tab per platform.</Empty>
      ) : (
        <div className="space-y-5">
          {data!.requests.length > 0 && (
            <div className="space-y-3">
              <h2 className="text-sm font-semibold uppercase tracking-wide text-muted">Decisions ({data!.requests.length})</h2>
              {data!.requests.map((r) => <RequestCard key={r.id} r={r} onDone={reload} />)}
            </div>
          )}
          {families.length > 0 && <p className="text-sm text-muted">{data!.items.length} version(s) across {families.length} post(s) waiting.</p>}
          {families.map((g) => <FamilyCard key={g.map((i) => `${i.id}-${i.version}`).join("|")} items={g} mode={data!.mode} liveEnabled={data!.live_publishing_enabled} onDone={reload} />)}
        </div>
      )}
    </div>
  );
}
