"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { useCan } from "@/components/auth";
import { ContentStatusBadge } from "@/components/domain";
import { Alert, Badge, Button, Card, Field, Modal, Select, Spinner, Textarea, useAction, useApi } from "@/components/ui";
import { api } from "@/lib/api";
import { fmtDate } from "@/lib/format";
import type { CampaignGeneration, DayStatus, GenerationDay, GenerationEstimate, GenerationStage } from "@/lib/types";

const STAGES: { id: GenerationStage; label: string }[] = [
  { id: "research", label: "Research" }, { id: "strategy", label: "Strategy" }, { id: "text", label: "Captions & prompts" },
  { id: "images", label: "Images" }, { id: "done", label: "Done" },
];
const DAY_TONE: Record<DayStatus, "neutral" | "info" | "ok" | "bad"> = { pending: "neutral", generating: "info", completed: "ok", failed: "bad" };
const ACTIVE = ["queued", "running"];
const usd = (v: number) => `$${v.toFixed(v < 1 ? 4 : 2)}`;

/** "Generate campaign" button and form: goal, store, days (default 5), platform, image shape, with a cost estimate. */
export function GenerateCampaignButton() {
  const can = useCan();
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState({ goal: "", days: 5, platform: "instagram", aspect_ratio: "4:5", brand_id: "" });
  const est = useApi<GenerationEstimate>(open ? `/api/campaign-generations/estimate?days=${form.days}` : null);
  const act = useAction();
  if (!can.edit) return null;
  const e = est.data;
  const start = async () => {
    const r = await act.run(() => api.post<CampaignGeneration>("/api/campaign-generations", {
      goal: form.goal, days: form.days, platform: form.platform, aspect_ratio: form.aspect_ratio,
      brand_id: form.brand_id ? Number(form.brand_id) : null,
    }));
    if (r) router.push(`/campaigns/${r.campaign_id}`);
  };
  return (
    <>
      <Button variant="primary" onClick={() => setOpen(true)}>Generate campaign</Button>
      <Modal open={open} onClose={() => setOpen(false)} title="Generate a research-backed campaign" wide>
        <Field label="Campaign goal" hint="e.g. Create a 5-day Diwali marketing campaign for my store.">{(id) =>
          <Textarea id={id} rows={3} value={form.goal} placeholder="Create a 5-day Diwali marketing campaign for my store."
            onChange={(ev) => setForm({ ...form, goal: ev.target.value })} />}</Field>
        <div className="grid gap-3 md:grid-cols-4">
          <Field label="Store">{(id) => (
            <Select id={id} value={form.brand_id} onChange={(ev) => setForm({ ...form, brand_id: ev.target.value })}>
              {(e?.brands || []).map((b) => <option key={b.id} value={b.id}>{b.name}</option>)}
            </Select>)}</Field>
          <Field label="Days">{(id) => (
            <Select id={id} value={form.days} onChange={(ev) => setForm({ ...form, days: Number(ev.target.value) })}>
              {[1, 2, 3, 4, 5, 6, 7].map((d) => <option key={d} value={d}>{d} day{d > 1 ? "s" : ""}</option>)}
            </Select>)}</Field>
          <Field label="Platform">{(id) => (
            <Select id={id} value={form.platform} onChange={(ev) => setForm({ ...form, platform: ev.target.value })}>
              <option value="instagram">Instagram</option><option value="facebook">Facebook</option>
            </Select>)}</Field>
          <Field label="Image shape">{(id) => (
            <Select id={id} value={form.aspect_ratio} onChange={(ev) => setForm({ ...form, aspect_ratio: ev.target.value })}>
              <option value="4:5">4:5 portrait</option><option value="1:1">1:1 square</option>
            </Select>)}</Field>
        </div>
        {est.error && <Alert tone="bad">{est.error}</Alert>}
        {e && (
          <div className="my-3 rounded-lg border border-line p-3 text-sm">
            <div className="font-semibold">Before you start</div>
            <ul className="mt-1 list-disc space-y-0.5 pl-5 text-muted">
              <li>{e.brave_requests ? `Up to ${e.brave_requests} Brave web searches for the whole campaign` : "No web searches (demo mode or research turned off)"}</li>
              <li>{e.llm_requests ? `${e.llm_requests} AI model calls (strategy, then captions and image prompts)` : "No AI model calls (demo mode uses the brand profile and catalogue only)"}</li>
              <li>{e.image_mode === "ai"
                ? `${e.image_requests} images with ${e.image_model} at ${e.image_size} (${usd(e.price_per_image_usd)} each; up to ${e.max_image_requests} if retried)`
                : "Images from the local poster renderer (AI images are off: set IMAGE_GENERATION_PROVIDER=gemini)"}</li>
              <li>Expected cost {usd(e.expected_cost_usd)} (at most {usd(e.max_cost_usd)}) of the {usd(e.budget_usd)} campaign budget.</li>
              <li>Real product photos are used as-is; AI only makes the background scene. Nothing is published without approval.</li>
            </ul>
            {!e.within_budget && <div className="mt-2"><Alert tone="warn">This is over the campaign budget. Use fewer days or raise CAMPAIGN_BUDGET_USD.</Alert></div>}
          </div>
        )}
        {act.error && <Alert tone="bad">{act.error}</Alert>}
        <Button variant="primary" busy={act.busy} disabled={form.goal.trim().length < 8 || (e ? !e.within_budget : false)} onClick={start}>Start generating</Button>
      </Modal>
    </>
  );
}

/** Progress, research, strategy and the per-day posts of a generated campaign. Polls while it's running. */
export function GenerationPanel({ campaignId }: { campaignId: number }) {
  const can = useCan();
  const { data, error, reload, setData } = useApi<{ generation: CampaignGeneration | null }>(`/api/campaigns/${campaignId}/generation`);
  const g = data?.generation;
  const running = !!g && (ACTIVE.includes(g.status) || g.day_items.some((d) => d.status === "generating"));
  const [now, setNow] = useState(0);
  useEffect(() => {
    if (!running) return;
    const t = setInterval(() => { reload(); setNow(Date.now()); }, 3000);
    return () => clearInterval(t);
  }, [running, reload]);
  const act = useAction();
  const [ack, setAck] = useState(false);
  if (error) return <div className="mb-5"><Alert tone="bad">Couldn&apos;t load the generation: {error}</Alert></div>;
  if (!g) return null;
  const update = (r?: CampaignGeneration) => { if (r) setData({ generation: r }); };
  const stageIndex = STAGES.findIndex((s) => s.id === g.stage);
  const waiting = g.status === "queued" && now > 0 && now - new Date(g.updated_at).getTime() > 90_000;
  const inReview = g.day_items.filter((d) => d.content?.status === "in_review").length;
  const done = g.day_items.filter((d) => d.status === "completed").length;

  return (
    <div className="mb-6 space-y-4">
      <Card title={<span className="flex items-center gap-2">AI campaign generation {running && <Spinner />}</span>}
        subtitle={g.goal}
        actions={<Badge tone={g.status === "completed" ? "ok" : g.status === "failed" ? "bad" : g.status === "partial" ? "warn" : "info"}>{g.status}</Badge>}>
        <ol className="flex flex-wrap gap-2 text-xs">
          {STAGES.map((s, i) => (
            <li key={s.id} className={`rounded-full border px-3 py-1 ${i < stageIndex || g.stage === "done" ? "border-ok/40 text-ok" : i === stageIndex ? (g.status === "failed" ? "border-bad text-bad" : "border-accent text-accent") : "border-line text-muted"}`}>
              {i + 1}. {s.label}{s.id === "images" && g.stage !== "research" ? ` (${done}/${g.days})` : ""}
            </li>
          ))}
        </ol>
        {g.status === "failed" && (
          <div className="mt-3 space-y-2">
            <Alert tone="bad" title={`Stopped at ${STAGES[stageIndex]?.label.toLowerCase() || g.stage}`}>{g.error}</Alert>
            {can.edit && <Button busy={act.busy} onClick={async () => update(await act.run(() => api.post<CampaignGeneration>(`/api/campaign-generations/${g.id}/retry`)))}>Retry from {STAGES[stageIndex]?.label.toLowerCase()}</Button>}
          </div>
        )}
        {waiting && <div className="mt-3"><Alert tone="warn">Waiting for the background worker to pick this up. Check that the API (and worker) is running.</Alert></div>}
        <div className="mt-3 grid gap-2 text-xs text-muted md:grid-cols-4">
          <div>Brave searches: <b className="text-ink">{g.usage.brave_requests}</b>{g.usage.brave_cached ? ` (+${g.usage.brave_cached} cached)` : ""}</div>
          <div>AI model calls: <b className="text-ink">{g.usage.llm_requests}</b> · {g.usage.llm_tokens.toLocaleString()} tokens</div>
          <div>Images: <b className="text-ink">{g.usage.images_generated}</b> made, {g.usage.image_requests} requested · {g.image_mode === "ai" ? g.image_model : "local renderer"}</div>
          <div>Estimated cost: <b className="text-ink">{usd(g.usage.cost_usd)}</b> of {usd(g.budget_usd)}</div>
        </div>
        {act.error && <div className="mt-3"><Alert tone="bad">{act.error}</Alert></div>}
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <ResearchCard g={g} />
        <StrategyCard g={g} />
      </div>

      {g.day_items.some((d) => d.content) && (
        <Card title={`${g.days}-day posts`} subtitle="Edit captions, retry a day's image, and approve each post. Nothing is published without approval."
          actions={can.own && inReview > 0 && (
            <div className="flex items-center gap-2">
              <label className="flex items-center gap-1.5 text-xs"><input type="checkbox" checked={ack} onChange={(ev) => setAck(ev.target.checked)} />I&apos;ve reviewed the warnings</label>
              <Button variant="ok" busy={act.busy} onClick={async () => update(await act.run(() => api.post<CampaignGeneration>(`/api/campaign-generations/${g.id}/approve-all`, { acknowledge_warnings: ack })))}>Approve {inReview} post{inReview > 1 ? "s" : ""}</Button>
            </div>
          )}>
          {g.result && g.result.skipped.length > 0 && (
            <div className="mb-3"><Alert tone="warn" title={`Approved day(s) ${g.result.approved_days.join(", ") || "none"}`}>
              {g.result.skipped.map((s) => <div key={s.day}>Day {s.day}: {s.reason}</div>)}
            </Alert></div>
          )}
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            {g.day_items.map((d) => <DayCard key={`${d.day}-${d.content?.version ?? 0}`} g={g} d={d} onChange={update} />)}
          </div>
        </Card>
      )}
    </div>
  );
}

function ResearchCard({ g }: { g: CampaignGeneration }) {
  const r = g.research;
  const sources = r.sources || [];
  return (
    <Card title="Research" subtitle={r.mode === "brave" ? `${sources.length} source(s) from ${r.queries?.length || 0} Brave search(es)` : r.mode === "demo" ? "Demo mode: no web searches" : g.stage === "research" ? "Not done yet" : "No web research"}>
      {(r.questions || []).length > 0 && (
        <div className="mb-3 text-sm"><div className="text-xs font-semibold uppercase text-muted">Questions</div>
          <ul className="list-disc pl-5">{r.questions.map((q) => <li key={q.query}>{q.question} <span className="text-xs text-muted">({q.query})</span></li>)}</ul></div>
      )}
      {g.strategy && g.strategy.findings.length > 0 && (
        <div className="mb-3 text-sm"><div className="text-xs font-semibold uppercase text-muted">Sourced findings</div>
          <ul className="list-disc pl-5">{g.strategy.findings.map((f) => <li key={f.finding}>{f.finding} <span className="text-xs text-muted">[{f.source_ids.join(", ")}]</span></li>)}</ul></div>
      )}
      {sources.length > 0 && (
        <div className="mb-3 text-sm"><div className="text-xs font-semibold uppercase text-muted">Sources</div>
          <ul className="space-y-1">{sources.map((s) => (
            <li key={s.id}><span className="mr-1 font-mono text-xs text-muted">{s.id}</span>
              <a href={s.url} target="_blank" rel="noopener noreferrer nofollow" className="text-accent hover:underline">{s.title || s.url}</a>
              {s.age && <span className="ml-1 text-xs text-muted">{s.age}</span>}</li>))}</ul></div>
      )}
      {[...(r.assumptions || []), ...(g.strategy?.assumptions || [])].length > 0 && (
        <div className="text-sm"><div className="text-xs font-semibold uppercase text-muted">Assumptions (not from sources)</div>
          <ul className="list-disc pl-5 text-muted">{[...(r.assumptions || []), ...(g.strategy?.assumptions || [])].map((a) => <li key={a}>{a}</li>)}</ul></div>
      )}
      {(r.errors || []).length > 0 && <div className="mt-2 text-xs text-warn">{r.errors!.length} search(es) failed: {r.errors!.map((e) => e.error).join("; ")}</div>}
    </Card>
  );
}

function StrategyCard({ g }: { g: CampaignGeneration }) {
  const s = g.strategy;
  if (!s) return <Card title="Strategy" subtitle={g.status === "failed" && g.stage === "strategy" ? "Failed; retry above" : "Not written yet"} />;
  return (
    <Card title="Strategy" subtitle={s.title}>
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
        <dt className="text-muted">Objective</dt><dd>{s.objective}</dd>
        <dt className="text-muted">Audience</dt><dd>{s.audience}</dd>
        <dt className="text-muted">Theme</dt><dd>{s.theme}</dd>
        <dt className="text-muted">Voice</dt><dd>{s.voice}</dd>
        <dt className="text-muted">Positioning</dt><dd>{s.positioning}</dd>
      </dl>
      <ol className="mt-3 space-y-1 text-sm">
        {s.days.map((d) => <li key={d.day}><b>Day {d.day}:</b> {d.concept} <span className="text-muted">— {d.objective}</span>{d.source_ids.length > 0 && <span className="text-xs text-muted"> [{d.source_ids.join(", ")}]</span>}</li>)}
      </ol>
    </Card>
  );
}

function DayCard({ g, d, onChange }: { g: CampaignGeneration; d: GenerationDay; onChange: (r?: CampaignGeneration) => void }) {
  const can = useCan();
  const act = useAction();
  const c = d.content;
  const [caption, setCaption] = useState(c?.caption || "");
  const [rejecting, setRejecting] = useState(false);
  const [note, setNote] = useState("");
  const base = `/api/campaign-generations/${g.id}/days/${d.day}`;
  const locked = c ? ["scheduled", "publishing", "published", "cancelled"].includes(c.status) : true;
  const refresh = async () => onChange(await api.get<{ generation: CampaignGeneration }>(`/api/campaigns/${g.campaign_id}/generation`).then((r) => r.generation ?? undefined));
  return (
    <div className="rounded-xl border border-line p-3">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <span className="font-semibold">Day {d.day}</span>
        <Badge tone={DAY_TONE[d.status]}>{d.status === "generating" ? "generating image…" : d.status}</Badge>
        {c && <ContentStatusBadge status={c.status} />}
        {c?.planned_at && <span className="text-xs text-muted">{fmtDate(c.planned_at, can.tz)}</span>}
      </div>
      <div className={`mb-2 flex items-center justify-center overflow-hidden rounded-lg bg-panel-2 ${g.aspect_ratio === "1:1" ? "aspect-square" : "aspect-[4/5]"}`}>
        {d.asset
          // eslint-disable-next-line @next/next/no-img-element
          ? <img src={d.asset.url} alt={d.asset.alt_text || `Day ${d.day} creative`} className="h-full w-full object-cover" loading="lazy" />
          : d.status === "generating" ? <Spinner /> : <span className="px-4 text-center text-xs text-muted">{d.status === "failed" ? "No image" : "Image not generated yet"}</span>}
      </div>
      {d.asset && <div className="mb-2 text-xs text-muted">{d.asset.width}×{d.asset.height} · {d.ai_asset ? `AI background (${g.image_model}) + real product photo` : "local poster renderer"}</div>}
      {d.status === "failed" && <div className="mb-2"><Alert tone="bad">{d.error}</Alert></div>}
      {c && (
        <>
          <div className="text-sm font-semibold">{c.hook}</div>
          {can.edit && !locked ? (
            <Textarea aria-label={`Day ${d.day} caption`} rows={5} value={caption} onChange={(ev) => setCaption(ev.target.value)} className="mt-1 text-sm" />
          ) : <p className="mt-1 whitespace-pre-wrap text-sm">{c.caption}</p>}
          <div className="mt-1 text-xs text-accent">{c.hashtags.join(" ")}</div>
          <div className="mt-1 text-xs"><span className="text-muted">CTA:</span> {c.cta}</div>
          {(c.validation_failures || 0) > 0 && <div className="mt-1 text-xs text-bad">{c.validation_failures} validation failure(s): open the post to fix them.</div>}
          {c.review_note && <div className="mt-1 text-xs text-warn">Changes requested: {c.review_note}</div>}
        </>
      )}
      {d.image_prompt?.prompt && <details className="mt-2 text-xs text-muted"><summary className="cursor-pointer">Image prompt</summary><p className="mt-1">{d.image_prompt.prompt}</p></details>}
      {act.error && <div className="mt-2"><Alert tone="bad">{act.error}</Alert></div>}
      <div className="mt-3 flex flex-wrap gap-2">
        {can.edit && c && !locked && caption !== c.caption && (
          <Button size="sm" variant="primary" busy={act.busy} onClick={async () => { if (await act.run(() => api.put(`/api/content/${c.id}`, { caption }))) await refresh(); }}>Save caption</Button>
        )}
        {can.edit && d.status === "failed" && <>
          <Button size="sm" busy={act.busy} onClick={async () => onChange(await act.run(() => api.post<CampaignGeneration>(`${base}/retry`, {})))}>Retry image</Button>
          <Button size="sm" variant="ghost" busy={act.busy} onClick={async () => onChange(await act.run(() => api.post<CampaignGeneration>(`${base}/local-poster`)))}>Use local poster</Button>
        </>}
        {can.edit && d.status === "completed" && !locked && (
          <Button size="sm" variant="ghost" busy={act.busy} onClick={async () => {
            if (g.image_mode !== "ai" || confirm(`Regenerate day ${d.day}'s image? This makes a new billable image request and the post needs approval again.`))
              onChange(await act.run(() => api.post<CampaignGeneration>(`${base}/retry`, { regenerate: true })));
          }}>Regenerate image</Button>
        )}
        {can.own && c?.status === "in_review" && <>
          <Button size="sm" variant="ok" busy={act.busy} onClick={async () => { if (await act.run(() => api.post(`/api/content/${c.id}/approve`, { version: c.version, acknowledge_warnings: confirm(`Approve day ${d.day} (v${c.version})? Confirm you've reviewed any warnings.`) }))) await refresh(); }}>Approve</Button>
          <Button size="sm" variant="danger" onClick={() => setRejecting(!rejecting)}>Reject</Button>
        </>}
        {c && <Link href={`/content/${c.id}`} className="self-center text-xs text-accent hover:underline">Open post →</Link>}
      </div>
      {rejecting && c && (
        <div className="mt-2 space-y-2">
          <Textarea aria-label="What should change?" rows={2} placeholder="What should change?" value={note} onChange={(ev) => setNote(ev.target.value)} />
          <Button size="sm" variant="danger" busy={act.busy} disabled={!note.trim()} onClick={async () => { if (await act.run(() => api.post(`/api/content/${c.id}/request-changes`, { note }))) { setRejecting(false); await refresh(); } }}>Send back</Button>
        </div>
      )}
    </div>
  );
}
