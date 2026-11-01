"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useCan } from "@/components/auth";
import { GenerationPanel } from "@/components/campaign-generation";
import { ContentTile } from "@/components/content-tile";
import { ContentStatusBadge, EvidenceBadge, Money, PlatformChip, SourceLink, SyntheticBadge } from "@/components/domain";
import { Alert, Badge, Button, Card, KeyValue, Loading, PageHeader, useAction, useApi } from "@/components/ui";
import { api } from "@/lib/api";
import { fmtDate, fmtPct, titleCase } from "@/lib/format";
import type { Campaign, ContentItem, Experiment, Finding, Hypothesis, Recommendation, ReportSummary } from "@/lib/types";

interface Detail {
  campaign: Campaign;
  items: ContentItem[];
  hypotheses: Hypothesis[];
  evidence: Finding[];
  experiments: Experiment[];
  recommendations: Recommendation[];
  reports: ReportSummary[];
}

const CRITERIA = ["relevance", "evidence_strength", "product_fit", "expected_value", "cost", "risk", "feasibility"];

export default function CampaignDetail() {
  const { id } = useParams<{ id: string }>();
  const can = useCan();
  const router = useRouter();
  const { data, loading, error } = useApi<Detail>(`/api/campaigns/${id}`);
  const rep = useAction();
  if (loading && !data) return <Loading />;
  if (error) return <Alert tone="bad">{error}</Alert>;
  if (!data) return null;
  const c = data.campaign;
  const ev = Object.fromEntries(data.evidence.map((f) => [f.id, f]));
  const families: ContentItem[][] = [];
  for (const i of data.items.filter((x) => !x.master_id)) families.push([i, ...data.items.filter((x) => x.master_id === i.id)]);
  const orphans = data.items.filter((x) => x.master_id && !data.items.some((m) => m.id === x.master_id));
  if (orphans.length) families.push(orphans);

  return (
    <div>
      <PageHeader title={c.name} subtitle={c.objective}
        actions={<>
          <SyntheticBadge show={c.is_synthetic} label="Demo data" />
          {can.edit && <Button busy={rep.busy} onClick={async () => { const r = await rep.run(() => api.post<ReportSummary>(`/api/campaigns/${c.id}/report`)); if (r) router.push(`/reports/${r.id}`); }}>Generate report</Button>}
          {c.run_id && <Link href={`/agent/${c.run_id}`} className="rounded-lg border border-line px-3.5 py-2 text-sm font-semibold hover:bg-panel-2">View agent run</Link>}
        </>} />
      {rep.error && <div className="mb-3"><Alert tone="bad">{rep.error}</Alert></div>}
      <GenerationPanel campaignId={c.id} />

      <div className="mb-5 grid gap-3 md:grid-cols-4">
        <Card><div className="text-xs font-semibold uppercase text-muted">Objective metric</div><div className="mt-1 font-semibold">{(c.objective_metric || "engagement_rate_by_reach").replace(/_/g, " ")}</div>
          <div className="text-xs text-muted">evaluate after {c.evaluation_window_days} days</div></Card>
        <Card><div className="text-xs font-semibold uppercase text-muted">Baseline</div><div className="mt-1 font-semibold">{c.baseline?.value != null ? fmtPct(c.baseline.value) : "none yet"}</div>
          <div className="text-xs text-muted">{c.baseline?.note || "The control variant is the comparison."}</div></Card>
        <Card><div className="text-xs font-semibold uppercase text-muted">Budget (AI & tools)</div><div className="mt-1 font-semibold">{c.budget_usd != null ? <Money usd={c.budget_usd} digits={2} /> : "workspace default"}</div>
          <div className="text-xs text-muted">{c.budget_allocation?.length ? `${c.budget_allocation.length} line item(s)` : "not estimated"}</div></Card>
        <Card><div className="text-xs font-semibold uppercase text-muted">Content</div><div className="mt-1 font-semibold">{families.length} idea(s) · {data.items.length} post(s)</div>
          <div className="text-xs text-muted">{Object.entries(data.items.reduce<Record<string, number>>((m, i) => ({ ...m, [i.status]: (m[i.status] || 0) + 1 }), {})).map(([k, v]) => `${v} ${k.replace("_", " ")}`).join(" · ")}</div></Card>
      </div>

      {c.channel_mix?.length > 0 && (
        <Card className="mb-5" title="Channel mix" subtitle="Why each platform is in the plan, and its share of posts. The A/B test runs on the primary channel.">
          <div className="space-y-2">
            {c.channel_mix.map((m) => (
              <div key={m.platform} className="grid items-center gap-2 text-sm md:grid-cols-[160px_1fr_2fr]">
                <span className="flex items-center gap-2"><PlatformChip platform={m.platform} withLabel /><Badge>{m.role}</Badge></span>
                <span className="flex items-center gap-2"><span className="h-2 flex-1 rounded bg-panel-2"><span className="block h-2 rounded bg-accent" style={{ width: `${Math.round(m.share * 100)}%` }} /></span><span className="w-10 text-right text-xs">{Math.round(m.share * 100)}%</span></span>
                <span className="text-xs text-muted">{m.reason}</span>
              </div>
            ))}
          </div>
          {c.budget_allocation?.length > 0 && (
            <div className="mt-4 border-t border-line pt-3 text-sm">
              <div className="mb-1 text-xs font-semibold uppercase text-muted">Estimated cost by item</div>
              {c.budget_allocation.map((b) => <div key={b.item} className="flex justify-between gap-2"><span>{b.item} <span className="text-xs text-muted">({b.note})</span></span><Money usd={b.usd} /></div>)}
            </div>
          )}
        </Card>
      )}

      {families.length > 0 && (
        <Card className="mb-5" title="Campaign gallery" subtitle="Each row is one idea; tiles are its platform versions, each with its own approval and publishing status." actions={<Link href={`/calendar`} className="text-sm text-accent hover:underline">Calendar →</Link>}>
          <div className="space-y-4">
            {families.map((g) => (
              <div key={g[0].id}>
                <div className="mb-1.5 flex flex-wrap items-center gap-2 text-sm"><strong>{g[0].pillar || g[0].title}</strong>{g[0].variant_label && <Badge tone="info">A/B {g[0].variant_label}</Badge>}<span className="text-xs text-muted">{fmtDate(g[0].planned_at, can.tz)}</span></div>
                <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-7">{g.map((i) => <ContentTile key={i.id} i={i} tz={can.tz} />)}</div>
              </div>
            ))}
          </div>
        </Card>
      )}

      <div className="grid gap-5 lg:grid-cols-3">
        <Card className="lg:col-span-2" title="Plan">
          <KeyValue items={[
            ["Audience", c.audience || "—"], ["Geography / language", `${c.geography || "—"} / ${c.language}`],
            ["Period", `${c.start_date || "—"} → ${c.end_date || "—"}`], ["Platforms", c.platforms.join(", ") || "—"],
            ["Pillars", c.pillars.join(" · ") || "—"], ["Creative direction", c.creative_direction || "—"],
            ["CTAs", c.ctas.join(" · ") || "—"], ["Measurement", c.measurement_plan || "—"],
            ["Success criteria", c.success_criteria.map((s) => `${titleCase(s.metric)}: ${s.comparator.replace(/_/g, " ")} ${s.target < 1 ? fmtPct(s.target, 0) : s.target}${s.variant ? ` (${s.variant} vs ${s.baseline})` : ""}`).join("; ") || "—"],
          ]} />
          {(c.risks.length > 0 || c.assumptions.length > 0) && (
            <div className="mt-3 grid gap-3 md:grid-cols-2 text-sm">
              <div><div className="mb-1 text-xs font-semibold uppercase text-muted">Risks</div><ul className="list-disc pl-4">{c.risks.map((r) => <li key={r}>{r}</li>)}</ul></div>
              <div><div className="mb-1 text-xs font-semibold uppercase text-muted">Assumptions</div><ul className="list-disc pl-4">{c.assumptions.map((r) => <li key={r}>{r}</li>)}</ul></div>
            </div>
          )}
        </Card>
        <Card title="Experiments">
          {data.experiments.length === 0 ? <p className="text-sm text-muted">None.</p> : data.experiments.map((e) => (
            <div key={e.id} className="mb-3 text-sm">
              <div className="flex flex-wrap items-center gap-1.5"><strong>{e.name}</strong><Badge tone={e.conclusion === "supported" ? "ok" : e.conclusion ? "warn" : "neutral"}>{e.conclusion || e.status}</Badge></div>
              <ul className="mt-1 text-xs text-muted">{e.variants.map((v) => <li key={v.label}>{v.label}: {v.description}</li>)}</ul>
              {e.learnings && <p className="mt-1 text-xs">{e.learnings}</p>}
            </div>
          ))}
          <Link href="/experiments" className="text-sm text-accent hover:underline">All experiments →</Link>
        </Card>
      </div>

      <Card className="mt-5" title="Campaign hypotheses" subtitle="Ranked on relevance, evidence strength, product fit, expected impact, cost, risk and feasibility (1–5 each, weighted). Heuristic scores that make the choice transparent — not probabilities.">
        {data.hypotheses.length === 0 ? <p className="text-sm text-muted">No hypotheses (manual campaign).</p> : (
          <div className="space-y-4">
            {data.hypotheses.map((h) => (
              <div key={h.id} className={`rounded-lg border p-3 ${h.selected ? "border-accent bg-accent-soft/40" : "border-line"}`}>
                <div className="flex flex-wrap items-center gap-2">
                  <Badge tone={h.selected ? "accent" : "neutral"}>#{h.rank}{h.selected ? " selected" : ""}</Badge>
                  <span className="text-sm font-semibold">{h.statement}</span>
                  <span className="ml-auto text-sm font-bold">{h.total_score.toFixed(2)}</span>
                </div>
                <div className="mt-2 grid gap-2 text-xs md:grid-cols-3">
                  <div><span className="text-muted">Mechanism:</span> {h.mechanism}</div>
                  <div><span className="text-muted">Uncertainty:</span> {h.uncertainty}</div>
                  <div><span className="text-muted">Validation:</span> {h.validation_method}</div>
                </div>
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {CRITERIA.map((k) => <Badge key={k} title={k}>{titleCase(k)} {h.scores[k] ?? "—"}</Badge>)}
                </div>
                {h.evidence_ids.length > 0 && (
                  <ul className="mt-2 space-y-1 text-xs">
                    {h.evidence_ids.map((eid) => ev[eid] && (
                      <li key={eid} className="flex flex-wrap items-center gap-1.5"><EvidenceBadge c={ev[eid].classification} /><SyntheticBadge show={ev[eid].is_synthetic} />
                        <span>{ev[eid].statement.slice(0, 160)}</span> <span className="text-muted">— <SourceLink f={ev[eid]} /></span></li>
                    ))}
                  </ul>
                )}
              </div>
            ))}
          </div>
        )}
      </Card>

      <Card className="mt-5" title="All posts (table)">
        {data.items.length === 0 ? <p className="text-sm text-muted">No content yet.</p> : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-left text-xs uppercase tracking-wide text-muted"><tr><th className="py-1.5 pr-3">Post</th><th className="pr-3">Platform</th><th className="pr-3">Type</th><th className="pr-3">Variant</th><th className="pr-3">Planned</th><th className="pr-3">Status</th><th>Checks</th></tr></thead>
              <tbody className="divide-y divide-line">
                {data.items.map((i) => (
                  <tr key={i.id}>
                    <td className="py-1.5 pr-3"><Link href={`/content/${i.id}`} className="font-medium hover:underline">{i.title || i.caption.slice(0, 60)}</Link></td>
                    <td className="pr-3"><PlatformChip platform={i.platform} /></td>
                    <td className="pr-3">{i.content_type}</td>
                    <td className="pr-3">{i.variant_label || "—"}</td>
                    <td className="pr-3 whitespace-nowrap text-muted">{fmtDate(i.published_at || i.scheduled_at || i.planned_at, can.tz)}</td>
                    <td className="pr-3"><ContentStatusBadge status={i.status} /></td>
                    <td>{i.validation ? (i.validation.ok ? <Badge tone="ok">pass</Badge> : <Badge tone="bad">{i.validation.failures} fail</Badge>) : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {(data.recommendations.length > 0 || data.reports.length > 0) && (
        <div className="mt-5 grid gap-5 md:grid-cols-2">
          <Card title="Recommendations">
            {data.recommendations.map((r) => <div key={r.id} className="mb-2 text-sm"><strong>{r.title}</strong><p className="text-muted">{r.detail}</p></div>)}
          </Card>
          <Card title="Reports">
            {data.reports.map((r) => <div key={r.id}><Link href={`/reports/${r.id}`} className="text-sm text-accent hover:underline">{r.title} · {fmtDate(r.created_at, can.tz)}</Link></div>)}
          </Card>
        </div>
      )}
    </div>
  );
}
