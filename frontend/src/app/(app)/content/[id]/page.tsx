"use client";

import { Download, RefreshCw } from "lucide-react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";
import { useAuth, useCan } from "@/components/auth";
import { ContentStatusBadge, EvidenceBadge, PLATFORM_ORDER, PLATFORMS, PlatformChip, SourceLink, SyntheticBadge, ValidationPanel } from "@/components/domain";
import { PlatformPreview } from "@/components/post-preview";
import { Alert, Badge, Button, Card, Field, Input, ListInput, Loading, Modal, PageHeader, Select, Textarea, useAction, useApi } from "@/components/ui";
import { api } from "@/lib/api";
import { diffWords } from "@/lib/diff";
import { cx, fmtDate, fmtNum, toLocalInput } from "@/lib/format";
import type { Asset, Connection, ContentItem, Product, Revision } from "@/lib/types";

const EDITABLE = ["draft", "in_review", "changes_requested", "approved", "scheduled", "failed"];
const TEMPLATES = ["", "product_showcase", "festive", "educational", "launch", "promo_poster", "awareness"];

function pick(item: ContentItem): Partial<ContentItem> {
  return { hook: item.hook, caption: item.caption, hashtags: item.hashtags, cta: item.cta, title: item.title, pillar: item.pillar,
    content_type: item.content_type, image_brief: item.image_brief, carousel_outline: item.carousel_outline, product_ids: item.product_ids,
    asset_id: item.asset_id };
}

export default function ContentEditor() {
  const { id } = useParams<{ id: string }>();
  const { data: item, error, loading, setData } = useApi<ContentItem>(`/api/content/${id}`);
  if (loading && !item) return <Loading />;
  if (error) return <Alert tone="bad">{error}</Alert>;
  if (!item) return null;
  // Remount the editor whenever a new version/status arrives, so local edits start from the saved item.
  return <Editor key={`${item.id}-${item.version}-${item.status}`} item={item} setData={setData} />;
}

function Diff({ a, b }: { a: string; b: string }) {
  const parts = diffWords(a, b);
  return (
    <p className="whitespace-pre-wrap text-sm">
      {parts.map((p, i) => <span key={i} className={cx(p.kind === "add" && "bg-ok-soft text-ok", p.kind === "del" && "bg-bad-soft text-bad line-through")}>{p.text}</span>)}
    </p>
  );
}

function CompareVersions({ revisions }: { revisions: Revision[] }) {
  const sorted = [...revisions].sort((x, y) => x.version - y.version);
  const [a, setA] = useState(sorted.length > 1 ? sorted[sorted.length - 2].version : sorted[0]?.version);
  const [b, setB] = useState(sorted[sorted.length - 1]?.version);
  const ra = sorted.find((r) => r.version === a);
  const rb = sorted.find((r) => r.version === b);
  if (!ra || !rb) return <p className="text-sm text-muted">Only one version so far.</p>;
  const field = (r: Revision, k: string) => {
    const v = r.snapshot[k];
    return Array.isArray(v) ? v.join(k === "carousel_outline" ? "\n" : " ") : v == null ? "" : String(v);
  };
  const keys = ["hook", "caption", "cta", "hashtags", "carousel_outline", "asset_id", "slide_asset_ids"];
  const changed = keys.filter((k) => field(ra, k) !== field(rb, k));
  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center gap-2 text-sm">
        Compare <Select aria-label="From version" className="w-auto" value={a} onChange={(e) => setA(Number(e.target.value))}>{sorted.map((r) => <option key={r.version} value={r.version}>v{r.version}</option>)}</Select>
        with <Select aria-label="To version" className="w-auto" value={b} onChange={(e) => setB(Number(e.target.value))}>{sorted.map((r) => <option key={r.version} value={r.version}>v{r.version}</option>)}</Select>
        <span className="text-xs text-muted">{rb.note}</span>
      </div>
      {changed.length === 0 ? <p className="text-sm text-muted">No content differences between these versions.</p> : (
        <div className="space-y-3">
          {changed.map((k) => (
            <div key={k}>
              <div className="mb-1 text-xs font-semibold uppercase text-muted">{k.replace(/_/g, " ")}</div>
              {k.includes("asset") ? (
                <div className="flex flex-wrap items-center gap-2">
                  {[ra, rb].map((r, idx) => {
                    const ids = k === "asset_id" ? [r.snapshot.asset_id as number] : ((r.snapshot.slide_asset_ids as number[]) || []);
                    return <div key={idx} className="flex gap-1">{ids.filter(Boolean).slice(0, 4).map((aid) => (
                      // eslint-disable-next-line @next/next/no-img-element
                      <img key={aid} src={`/api/assets/${aid}/file`} alt={`v${r.version} image`} className="h-20 w-16 rounded border border-line object-cover" />))}
                      {idx === 0 && <span className="self-center text-muted">→</span>}</div>;
                  })}
                </div>
              ) : <Diff a={field(ra, k)} b={field(rb, k)} />}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function CreativePanel({ item, editable, onSaved }: { item: ContentItem; editable: boolean; onSaved: (i: ContentItem) => void }) {
  const { state } = useAuth();
  const [variation, setVariation] = useState(0);
  const [template, setTemplate] = useState("");
  const [ai, setAi] = useState(false);
  const act = useAction();
  const assets = item.assets || [];
  const cover = assets[0];
  const checks = cover?.meta?.checks || [];
  const canRender = editable && ["post", "carousel"].includes(item.content_type);
  const render = async () => {
    const r = await act.run(() => api.post<ContentItem>(`/api/content/${item.id}/creative`, { variation, template: template || null, ai_background: ai }));
    if (r) onSaved(r);
  };
  return (
    <Card title="Creative" subtitle={cover?.meta ? `${cover.meta.template?.replace(/_/g, " ")} · ${cover.meta.format?.width}×${cover.meta.format?.height} (${cover.meta.format?.label}) · ${cover.source_label}` : "No rendered image yet."}
      actions={cover && <>
        <a className="inline-flex items-center gap-1 rounded-lg border border-line px-2.5 py-1 text-xs font-semibold hover:bg-panel-2" href={`/api/assets/${cover.id}/file?format=png&download=1`}><Download className="h-3.5 w-3.5" />PNG</a>
        <a className="inline-flex items-center gap-1 rounded-lg border border-line px-2.5 py-1 text-xs font-semibold hover:bg-panel-2" href={`/api/assets/${cover.id}/file?format=jpg&download=1`}><Download className="h-3.5 w-3.5" />JPG</a>
      </>}>
      {assets.length > 0 && (
        <div className="mb-3 flex gap-2 overflow-x-auto">
          {assets.map((a: Asset, i) => (
            <a key={a.id} href={`/assets?focus=${a.id}`} className="shrink-0" title={a.filename}>
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img src={a.url} alt={a.alt_text || `slide ${i + 1}`} className="h-28 rounded border border-line bg-panel-2 object-cover" style={{ aspectRatio: a.width && a.height ? `${a.width}/${a.height}` : "4/5" }} />
            </a>
          ))}
        </div>
      )}
      {cover?.meta && (
        <div className="mb-3 space-y-1 text-xs">
          <div className="flex flex-wrap gap-1.5">{checks.map((c, i) => <Badge key={i} tone={c.status === "pass" ? "ok" : c.status === "warn" ? "warn" : "bad"} title={c.message}>{c.status === "pass" ? "✓" : c.status === "warn" ? "!" : "✕"} {c.code.replace(/_/g, " ")}</Badge>)}</div>
          {(cover.meta.notes || []).map((n) => <p key={n} className="text-muted">{n}</p>)}
          {cover.meta.text_sources && <p className="text-muted">Text sources: {Object.entries(cover.meta.text_sources).filter(([, v]) => v).map(([k, v]) => `${k} — ${v}`).join("; ")}</p>}
        </div>
      )}
      {canRender && (
        <div className="flex flex-wrap items-end gap-2 border-t border-line pt-3">
          <Field label="Layout variation">{(fid) => <Select id={fid} value={variation} onChange={(e) => setVariation(Number(e.target.value))}>{[0, 1, 2, 3, 4, 5].map((v) => <option key={v} value={v}>Variation {v + 1}</option>)}</Select>}</Field>
          <Field label="Template">{(fid) => <Select id={fid} value={template} onChange={(e) => setTemplate(e.target.value)}>{TEMPLATES.map((t) => <option key={t} value={t}>{t ? t.replace(/_/g, " ") : "Automatic"}</option>)}</Select>}</Field>
          <label className="mb-3 flex items-center gap-1.5 text-xs" title={state?.mode === "demo" ? "Demo mode makes no external calls; the template background is used." : "Uses the configured image model (costs money; labelled on the image)."}>
            <input type="checkbox" checked={ai} onChange={(e) => setAi(e.target.checked)} />AI background</label>
          <Button className="mb-3" busy={act.busy} onClick={render}><RefreshCw className="h-3.5 w-3.5" />Re-render (new version)</Button>
        </div>
      )}
      {act.error && <Alert tone="bad">{act.error}</Alert>}
    </Card>
  );
}

type PosterStatus = {
  job: { id: number; status: string; calls_used: number; error: string } | null;
  poster: ContentItem["canva_poster"];
};

function CanvaPosterPanel({ item, editable, onSaved, imageBrief, onPromptChange, references }: {
  item: ContentItem; editable: boolean; onSaved: (i: ContentItem) => void;
  imageBrief: string; onPromptChange: (prompt: string) => void; references: Asset[];
}) {
  const path = `/api/content/${item.id}/canva-poster`;
  const posterState = useApi<PosterStatus>(path);
  const connection = useApi<{ status: string; message?: string }>("/api/integrations/canva/status");
  const act = useAction();
  const fallback = useAction();
  const edit = useAction();
  const [editLink, setEditLink] = useState<string | null>(null);
  const upload = useAction();
  const [referenceId, setReferenceId] = useState<number | null>(item.canva_poster?.reference_asset_id || null);
  const [uploadedReference, setUploadedReference] = useState<Asset | null>(null);
  const actionError = useRef<HTMLDivElement>(null);
  const referenceOptions = references.filter((asset) => asset.kind !== "preview" && ["image/png", "image/jpeg"].includes(asset.content_type));
  if (uploadedReference && !referenceOptions.some((asset) => asset.id === uploadedReference.id)) referenceOptions.unshift(uploadedReference);
  useEffect(() => {
    if (act.error) actionError.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [act.error]);
  const uploadReference = async (file?: File) => {
    if (!file) return;
    const result = await upload.run(async () => {
      if (!["image/png", "image/jpeg"].includes(file.type) || file.size > 8 * 1024 * 1024) {
        throw new Error("Choose a PNG or JPEG image up to 8 MB.");
      }
      const form = new FormData();
      form.append("file", file);
      form.append("alt_text", "User-provided reference image for AI design variations.");
      return api.upload<Asset>("/api/assets", form);
    });
    if (result) { setUploadedReference(result); setReferenceId(result.id); }
  };
  const job = posterState.data?.job;
  const poster = posterState.data?.poster || item.canva_poster;
  const active = !!job && ["queued", "generating", "exporting", "qa"].includes(job.status);
  const connected = connection.data?.status === "connected";
  const choosing = job?.status === "awaiting_selection";
  const options = poster?.candidates || [];
  const promptChanged = (poster?.prompt !== undefined && imageBrief.trim() !== poster.prompt.trim()) || (poster?.reference_asset_id || null) !== referenceId;
  const stepNames = ["Generate options", "Choose a design", "Create design", "Export", "In review"];
  const stepIndex = choosing ? 1 : job?.status === "exporting" || job?.status === "qa" ? 3
    : job?.status === "completed" ? 4 : active ? (poster?.selected_candidate_id ? 2 : 0) : -1;
  const imageId = poster?.asset_id || (job?.status === "completed" ? item.asset_id : null);
  const imageUrl = item.assets?.find((asset) => asset.id === imageId)?.url || (imageId ? `/api/assets/${imageId}/file` : null);

  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(posterState.reload, 1800);
    return () => window.clearInterval(timer);
  }, [active, posterState.reload]);

  useEffect(() => {
    if (job?.status !== "completed" || !imageId || item.asset_id === imageId) return;
    void api.get<ContentItem>(`/api/content/${item.id}`).then(onSaved).catch(() => undefined);
  }, [job?.status, imageId, item.asset_id, item.id, onSaved]);

  const requestPoster = async () => {
    const result = await act.run(() => api.post<{ job_id: number; status: string; calls_used: number }>(path, {
      prompt: imageBrief, reference_asset_id: referenceId, regenerate: choosing || job?.status === "completed",
    }));
    if (result) void posterState.reload();
  };
  const chooseOption = async (candidateId: string) => {
    if (!poster?.request_key) return;
    const result = await act.run(() => api.post(`${path}/select`, {
      candidate_id: candidateId, request_key: poster.request_key,
    }));
    if (result) void posterState.reload();
  };
  const renderFallback = async () => {
    const result = await fallback.run(() => api.post<ContentItem>(`/api/content/${item.id}/creative`, {}));
    if (result) onSaved(result);
  };
  const fetchEditLink = async () => {
    const result = await edit.run(() => api.get<{ edit_url: string }>(`/api/content/${item.id}/canva-poster/edit-link`));
    if (result?.edit_url) setEditLink(result.edit_url);
  };
  const unavailable = !connected;
  const canUseRenderer = editable && ["post", "carousel"].includes(item.content_type);

  return (
    <Card title="Create images with Canva" subtitle="Describe your image, compare the options, then choose one to create and export.">
      <Field label="Image brief" hint="Describe the subject, style, colours, layout and any text. No separate AI Workspace brief is needed.">
        {(fid) => <Textarea id={fid} rows={5} maxLength={5000} value={imageBrief}
          disabled={!editable || active} onChange={(event) => onPromptChange(event.target.value)}
          placeholder="For example: A warm, minimal festive poster with gold abstract shapes, cream background and bold lettering. Give me different visual directions." />}
      </Field>
      <div className="my-3 rounded-xl border border-line p-3">
        <Field label="Reference image (optional)" hint="Use an image to guide new AI variations. Describe what to change in the Image brief.">
          {(fid) => <Select id={fid} value={referenceId || ""} disabled={!editable || active || upload.busy}
            onChange={(event) => setReferenceId(event.target.value ? Number(event.target.value) : null)}>
            <option value="">No reference — text to image</option>
            {referenceOptions.map((asset) => <option key={asset.id} value={asset.id}>{asset.filename}</option>)}
          </Select>}
        </Field>
        <label className={cx("mt-3 inline-flex rounded-lg border border-line px-3 py-2 text-xs font-semibold", active || upload.busy || !editable ? "opacity-50" : "cursor-pointer hover:bg-panel-2")}>
          {upload.busy ? "Uploading reference…" : "Upload a reference image"}
          <input type="file" aria-label="Upload a reference image" accept="image/png,image/jpeg" className="sr-only"
            disabled={!editable || active || upload.busy}
            onChange={(event) => { void uploadReference(event.target.files?.[0]); event.target.value = ""; }} />
        </label>
        {referenceId && <div className="mt-3">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src={`/api/assets/${referenceId}/file`} alt="Reference image for Canva variations" className="max-h-40 max-w-full rounded-lg object-contain" />
          <p className="mt-2 text-xs text-muted">AI may change image details. Generated options require review and are never treated as verified product photos.</p>
        </div>}
        {upload.error && <Alert tone="bad">{upload.error}</Alert>}
      </div>
      <p className="my-2 text-xs text-muted">Your Image brief is saved when you generate options. AI-generated designs always need human review.</p>
      {connection.loading ? <p className="mb-3 text-sm text-muted">Checking Canva connection…</p> : unavailable ? (
        <Alert tone="warn" title="Canva is unavailable for this workspace">
          {connection.data?.message || connection.error || "Connect Canva to generate editable designs."} {" "}
          <Link href="/integrations" className="underline">Open integrations</Link>
        </Alert>
      ) : null}
      {posterState.error && <div className="mb-3"><Alert tone="bad">{posterState.error}</Alert></div>}
      {job?.status === "failed" && <Alert tone="bad" title="Canva poster failed">{job.error || poster?.error || "The design could not be completed."}</Alert>}

      {job && (
        <div className="my-4 rounded-xl border border-line bg-panel-2 p-3">
          <div className="mb-2 flex flex-wrap items-center gap-2">
            <span className="text-sm font-semibold">Job {job.id}</span>
            <Badge tone={job.status === "failed" ? "bad" : job.status === "completed" ? "ok" : "info"}>{job.status.replace(/_/g, " ")}</Badge>
            {poster?.mode && <Badge>{poster.mode}</Badge>}
          </div>
          {job.status !== "failed" ? (
            <ol aria-label="Canva poster progress" className="grid gap-2 text-xs sm:grid-cols-5">
              {stepNames.map((name, index) => <li key={name} className={cx("rounded-lg border px-2 py-2", index < stepIndex ? "border-ok/30 bg-ok-soft text-ok" : index === stepIndex ? "border-accent bg-accent-soft text-accent" : "border-line text-muted")}>
                <span className="mr-1" aria-hidden>{index < stepIndex ? "✓" : index === stepIndex ? "●" : "○"}</span>{name}
              </li>)}
            </ol>
          ) : <div className="mt-2 text-xs text-muted">Design ID: {poster?.design_id || "not created"}</div>}
        </div>
      )}

      {choosing && <div className="my-4">
        <h3 className="mb-2 font-semibold">Choose your favourite design</h3>
        <p className="mb-3 text-sm text-muted">These are previews. Only the option you choose will be created and exported.</p>
        {promptChanged && <Alert tone="warn">You changed the Image brief or reference. Generate new options to use your changes.</Alert>}
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          {options.map((option, index) => <div key={option.id} className="overflow-hidden rounded-xl border border-line bg-panel-2 p-3">
            {option.preview_asset_id || option.preview_url ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img src={option.preview_asset_id ? `/api/assets/${option.preview_asset_id}/file` : option.preview_url} referrerPolicy="no-referrer" alt={`Canva design option ${index + 1}`}
                className="mb-3 aspect-[4/5] w-full rounded-lg bg-panel object-contain" />
            ) : <div className="mb-3 flex aspect-[4/5] items-center justify-center rounded-lg bg-panel text-sm text-muted">Preview unavailable</div>}
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="text-sm font-semibold">{option.title}</span>
              <Button busy={act.busy} disabled={!editable || promptChanged || active}
                onClick={() => chooseOption(option.id)}>Use option {index + 1}</Button>
            </div>
          </div>)}
        </div>
      </div>}

      {imageUrl && <div className="my-3 max-w-sm overflow-hidden rounded-xl border border-line bg-panel-2">
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src={imageUrl} alt="Generated Canva poster awaiting human review" className="block h-auto w-full" />
        <a href={`${imageUrl}?download=1`} className="block border-t border-line p-2 text-center text-xs font-semibold text-accent hover:underline">Download PNG</a>
      </div>}

      {poster?.qa && <div className="my-3 rounded-xl border border-line p-3">
        <div className="mb-2 flex items-center gap-2"><span className="text-sm font-semibold">Quality review</span>
          <Badge tone={poster.qa.passed ? "ok" : "warn"}>{poster.qa.status.replace(/_/g, " ")}</Badge>
        </div>
        {poster.qa.issues.length > 0 && <ul className="list-disc space-y-1 pl-5 text-xs text-muted">{poster.qa.issues.map((issue, index) => <li key={`${index}-${issue}`}>{issue}</li>)}</ul>}
      </div>}

      {poster?.image_skip_reason && <p className="mb-2 text-xs text-muted">{poster.image_skip_reason}</p>}
      {poster?.design_id && <p className="mb-2 break-all text-xs text-muted">Canva design ID: {poster.design_id}</p>}
      {poster?.design_id && !editLink && <Button size="sm" busy={edit.busy} onClick={fetchEditLink}>Get Canva edit link</Button>}
      {editLink && <a href={editLink} target="_blank" rel="noopener noreferrer" className="ml-2 inline-flex rounded-lg border border-line px-2.5 py-1 text-xs font-semibold text-accent hover:bg-panel-2">Open in Canva ↗</a>}
      {edit.error && <p className="mt-2 text-xs text-bad">{edit.error}</p>}

      {(job?.status === "failed" || unavailable) && canUseRenderer && (
        <div className="mt-3 rounded-xl border border-line p-3">
          <p className="mb-2 text-xs text-muted">Use GrowthPilot&apos;s built-in, claim-checked renderer instead. Its output also requires human approval.</p>
          <Button busy={fallback.busy} onClick={renderFallback}>Generate with built-in renderer</Button>
          {fallback.error && <p className="mt-2 text-xs text-bad">{fallback.error}</p>}
        </div>
      )}

      {editable && <div className="mt-3 flex flex-wrap gap-2">
        <Button variant="primary" busy={act.busy} disabled={!connected || active || upload.busy || !imageBrief.trim()}
          onClick={requestPoster}>{job?.status === "failed" && !promptChanged ? "Retry" : choosing || job?.status === "completed" ? "Generate new options" : "Generate options"}</Button>
      </div>}
      {act.error && <div ref={actionError} role="alert" className="mt-3"><Alert tone="bad">{act.error}</Alert></div>}
      {item.status === "in_review" && <p className="mt-3 text-xs font-semibold text-warn">Human approval is still required before any publishing.</p>}
    </Card>
  );
}

function Editor({ item, setData }: { item: ContentItem; setData: (i: ContentItem) => void }) {
  const id = item.id;
  const can = useCan();
  const router = useRouter();
  const { state } = useAuth();
  const products = useApi<Product[]>("/api/products");
  const library = useApi<Asset[]>("/api/assets?source=upload,catalogue,rendered");
  const integ = useApi<{ connections: Connection[]; live_publishing_enabled: boolean }>("/api/integrations");
  const [draft, setDraft] = useState<Partial<ContentItem>>(() => pick(item));
  const [regenOpen, setRegenOpen] = useState(false);
  const [variantsOpen, setVariantsOpen] = useState(false);
  const [newPlatforms, setNewPlatforms] = useState<string[]>([]);
  const [instructions, setInstructions] = useState("");
  const [schedState, setSched] = useState(() => ({ connection_id: "", when: toLocalInput(item.planned_at, can.tz), confirm: false }));
  const act = useAction();

  const conns = useMemo(() => (integ.data?.connections || []).filter((c) => c.available_in_mode && ["connected", "demo"].includes(c.status) && (c.platform === "any" || c.platform === item.platform)), [integ.data, item.platform]);
  const sched = { ...schedState, connection_id: schedState.connection_id || (conns[0] ? String(conns[0].id) : "") };

  const editable = can.edit && EDITABLE.includes(item.status);
  const dirty = JSON.stringify(draft) !== JSON.stringify(pick(item));
  const doAct = async (fn: () => Promise<ContentItem | { item: ContentItem }>) => {
    const r = await act.run(fn);
    if (r) setData("item" in r ? r.item : r);
  };
  const save = () => doAct(() => api.put<ContentItem>(`/api/content/${id}`, draft));
  const selectedConn = conns.find((c) => String(c.id) === sched.connection_id);
  const liveTarget = !!selectedConn?.adapter_info?.external;
  const family = item.family || [];
  const isVariant = !!item.master_id;
  const existing = new Set(family.map((f) => f.platform));
  const account = item.brand_name || state?.workspace?.name?.replace(/ \(demo\)$/, "") || "brand";

  return (
    <div>
      <PageHeader title={item.title || `Post #${item.id}`}
        subtitle={<span className="flex flex-wrap items-center gap-2"><PlatformChip platform={item.platform} withLabel /><ContentStatusBadge status={item.status} /> v{item.version} · {item.content_type}
          {item.campaign_id && <> · <Link href={`/campaigns/${item.campaign_id}`} className="text-accent hover:underline">{item.campaign_name}</Link></>}
          {isVariant && <Badge tone="info">variant of #{item.master_id}</Badge>}
          {item.variant_label && <Badge>A/B {item.variant_label}</Badge>}<SyntheticBadge show={item.is_synthetic} label="Demo" />
          {item.generated_by !== "human" && <Badge>{item.generated_by}</Badge>}</span>} />

      {family.length > 1 && (
        <div role="tablist" aria-label="Platform versions" className="mb-4 flex flex-wrap gap-1 border-b border-line">
          {family.map((f) => (
            <button key={f.id} role="tab" aria-selected={f.id === item.id} onClick={() => f.id !== item.id && router.push(`/content/${f.id}`)}
              className={cx("-mb-px flex items-center gap-1.5 border-b-2 px-3 py-2 text-sm font-semibold", f.id === item.id ? "border-accent text-accent" : "border-transparent text-muted hover:text-ink")}>
              <PlatformChip platform={f.platform} />{PLATFORMS[f.platform]?.label || f.platform}<ContentStatusBadge status={f.status} />
            </button>
          ))}
        </div>
      )}

      {act.error && <div className="mb-3"><Alert tone="bad">{act.error}</Alert></div>}
      {item.review_note && <div className="mb-3"><Alert tone="warn" title="Reviewer note">{item.review_note}</Alert></div>}
      {item.last_error && <div className="mb-3"><Alert tone={item.status === "failed" ? "bad" : "warn"}>{item.last_error}</Alert></div>}
      {item.approved_version && item.approved_version !== item.version && <div className="mb-3"><Alert tone="warn">Approved version was v{item.approved_version}; this is v{item.version}. It needs approval again.</Alert></div>}
      {item.approval_intact === true && ["approved", "scheduled"].includes(item.status) && <div className="mb-3"><Alert tone="ok">Approval covers this exact text and these exact image files (hash verified).</Alert></div>}
      {item.approval_intact === false && <div className="mb-3"><Alert tone="bad">The text or image files changed after approval. Publishing will be refused until it&apos;s approved again.</Alert></div>}

      <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_400px]">
        <div className="space-y-5">
          <Card title="Content" actions={editable && <>
            <Button size="sm" onClick={() => setRegenOpen(true)}>{isVariant ? `Regenerate ${item.platform} version` : "Regenerate"}</Button>
            {!isVariant && <Button size="sm" onClick={() => setVariantsOpen(true)}>Add platforms</Button>}
            <Button size="sm" variant="primary" busy={act.busy} disabled={!dirty} onClick={save}>Save as v{item.version + 1}</Button></>}>
            {!editable && <p className="mb-2 text-xs text-muted">This item can&apos;t be edited in its current status.</p>}
            {item.platform_spec && <p className="mb-3 text-xs text-muted">{item.platform_spec.label}: up to {item.platform_spec.caption_max.toLocaleString("en-IN")} characters{item.platform_spec.hashtags_max !== null && `, ${item.platform_spec.hashtags_max} hashtags`}. Tone: {item.platform_spec.tone}</p>}
            <fieldset disabled={!editable} className="grid gap-x-4 md:grid-cols-2">
              <Field label="Internal title">{(fid) => <Input id={fid} value={draft.title || ""} onChange={(e) => setDraft({ ...draft, title: e.target.value })} />}</Field>
              <div className="grid grid-cols-2 gap-3">
                <Field label="Type">{(fid) => <Select id={fid} value={draft.content_type} onChange={(e) => setDraft({ ...draft, content_type: e.target.value })}>{["post", "carousel", "text", "video", "reel", "story"].map((t) => <option key={t}>{t}</option>)}</Select>}</Field>
                <Field label="Pillar">{(fid) => <Input id={fid} value={draft.pillar || ""} onChange={(e) => setDraft({ ...draft, pillar: e.target.value })} />}</Field>
              </div>
              <div className="md:col-span-2"><Field label="Hook" hint="also the poster headline">{(fid) => <Input id={fid} value={draft.hook || ""} onChange={(e) => setDraft({ ...draft, hook: e.target.value })} />}</Field></div>
              <div className="md:col-span-2"><Field label="Caption">{(fid) => <Textarea id={fid} rows={7} value={draft.caption || ""} onChange={(e) => setDraft({ ...draft, caption: e.target.value })} />}</Field></div>
              <Field label="Call to action">{(fid) => <Input id={fid} value={draft.cta || ""} onChange={(e) => setDraft({ ...draft, cta: e.target.value })} />}</Field>
              <Field label="Hashtags" hint="space separated">{(fid) => <Input id={fid} value={(draft.hashtags || []).join(" ")} onChange={(e) => setDraft({ ...draft, hashtags: e.target.value.split(/\s+/).filter(Boolean) })} />}</Field>
              <div className="md:col-span-2"><Field label="Products featured" hint="claims are checked against these">{(fid) => (
                <select id={fid} multiple className="h-28 w-full rounded-lg border border-line bg-panel px-2 py-1 text-sm" value={(draft.product_ids || []).map(String)}
                  onChange={(e) => setDraft({ ...draft, product_ids: Array.from(e.target.selectedOptions).map((o) => Number(o.value)) })}>
                  {products.data?.map((p) => <option key={p.id} value={p.id}>{p.name}{p.price !== null ? ` · ₹${p.price}` : ""}</option>)}
                </select>)}</Field></div>
              <div className="md:col-span-2"><Field label="Cover image" hint="real photos you uploaded, the store's own photos, or rendered posters — never stock images">{(fid) => (
                <Select id={fid} value={draft.asset_id ?? ""} onChange={(e) => setDraft({ ...draft, asset_id: e.target.value ? Number(e.target.value) : null })}>
                  <option value="">None</option>
                  {library.data?.map((a) => <option key={a.id} value={a.id}>{a.source_label}: {a.filename}</option>)}
                </Select>)}</Field></div>
              {draft.content_type === "carousel" && <div className="md:col-span-2"><Field label="Carousel outline" hint="one slide per line; re-render to update slides">{() => <ListInput value={draft.carousel_outline || []} onChange={(v) => setDraft({ ...draft, carousel_outline: v })} rows={5} />}</Field></div>}
            </fieldset>
            {item.rationale && <p className="mt-2 text-xs text-muted">Rationale: {item.rationale}</p>}
          </Card>

          <CreativePanel item={item} editable={editable} onSaved={setData} />
          <CanvaPosterPanel item={item} editable={editable} onSaved={setData}
            imageBrief={draft.image_brief || ""} onPromptChange={(prompt) => setDraft({ ...draft, image_brief: prompt })} references={library.data || []} />

          <Card title="Verification" actions={<Button size="sm" busy={act.busy} onClick={async () => { const v = await act.run(() => api.post(`/api/content/${id}/validate`)); if (v) setData({ ...item, validation: v as ContentItem["validation"] }); }}>Re-check</Button>}>
            <ValidationPanel v={item.validation} version={item.version} />
            {(item.products || []).length > 0 && (
              <div className="mt-3 border-t border-line pt-2 text-xs text-muted">
                Facts checked against: {item.products!.map((p) => <span key={p.id} className="mr-2">{p.source_url ? <a href={p.source_url} target="_blank" rel="noopener noreferrer" className="text-info hover:underline">{p.name}</a> : p.name} ({p.freshness?.state})</span>)}
              </div>
            )}
          </Card>

          {item.evidence && item.evidence.length > 0 && (
            <Card title="Evidence behind this post" subtitle={item.hypothesis?.statement}>
              <ul className="space-y-1.5 text-sm">
                {item.evidence.map((f) => <li key={f.id} className="flex flex-wrap items-center gap-1.5"><EvidenceBadge c={f.classification} /><SyntheticBadge show={f.is_synthetic} />{f.statement.slice(0, 200)}<span className="text-xs text-muted">— <SourceLink f={f} /></span></li>)}
              </ul>
            </Card>
          )}

          <Card title="Version history">
            {(item.revisions?.length || 0) > 1 && <div className="mb-4 rounded-lg border border-line p-3"><CompareVersions revisions={item.revisions!} /></div>}
            <ul className="divide-y divide-line text-sm">
              {item.revisions?.map((r) => (
                <li key={r.version} className="flex flex-wrap items-center justify-between gap-2 py-1.5">
                  <span><strong>v{r.version}</strong> · {r.source} {r.note && <span className="text-muted">— {r.note}</span>}{item.approved_version === r.version && <Badge tone="accent" className="ml-1">approved</Badge>}</span>
                  <span className="flex items-center gap-2 text-xs text-muted">{fmtDate(r.created_at, can.tz)}
                    {editable && r.version !== item.version && <Button size="sm" variant="ghost" onClick={() => doAct(() => api.post<ContentItem>(`/api/content/${id}/restore`, { version: r.version }))}>Restore</Button>}</span>
                </li>
              ))}
            </ul>
          </Card>
        </div>

        <div className="space-y-5">
          <Card title={`${PLATFORMS[item.platform]?.label || item.platform} preview`}>
            <PlatformPreview platform={item.platform} assets={item.assets || []} caption={item.final_caption || ""} account={account} type={item.content_type} title={item.hook} limit={item.platform_spec?.caption_max} />
          </Card>
          <Card title="Workflow">
            <div className="space-y-2 text-sm">
              <div>Planned: {fmtDate(item.planned_at, can.tz)}</div>
              {item.scheduled_at && <div>Scheduled: {fmtDate(item.scheduled_at, can.tz)} via {item.connection_name}</div>}
              {item.published_at && <div>Published: {fmtDate(item.published_at, can.tz)} {item.publish_verified ? <Badge tone="ok">verified by provider</Badge> : <Badge tone="warn">not verified</Badge>}</div>}
              {item.external_url && <a href={item.external_url} target="_blank" rel="noopener noreferrer" className="text-info hover:underline">View post ↗</a>}
              <div className="flex flex-wrap gap-2 pt-1">
                {can.edit && ["draft", "changes_requested"].includes(item.status) && <Button variant="primary" busy={act.busy} disabled={dirty} onClick={() => doAct(() => api.post<ContentItem>(`/api/content/${id}/submit`))}>Submit for review</Button>}
                {item.status === "in_review" && <Link href="/approvals" className="rounded-lg bg-accent px-3.5 py-2 text-sm font-semibold text-accent-ink">Review in Approval Inbox</Link>}
                {can.own && item.status === "scheduled" && <>
                  <Button busy={act.busy} onClick={() => doAct(() => api.post<ContentItem>(`/api/content/${id}/unschedule`))}>Unschedule</Button>
                  <Button variant={item.connection_live ? "danger" : "primary"} busy={act.busy} onClick={() => {
                    const live = item.connection_live;
                    if (live && !confirm("This publishes to a REAL account now. Continue?")) return;
                    void doAct(() => api.post<ContentItem>(`/api/content/${id}/publish-now`, { confirm: live }));
                  }}>Publish now</Button></>}
                {item.status === "published" && <Button size="sm" busy={act.busy} onClick={() => doAct(() => api.post<{ item: ContentItem }>(`/api/content/${id}/verify`))}>Re-verify with provider</Button>}
                {can.edit && !["published", "publishing", "cancelled"].includes(item.status) && <Button variant="ghost" onClick={() => { if (confirm("Cancel this post?")) void doAct(() => api.post<ContentItem>(`/api/content/${id}/cancel`)); }}>Cancel post</Button>}
              </div>
            </div>
            {can.own && ["approved", "failed"].includes(item.status) && item.approved_version === item.version && (
              <div className="mt-4 border-t border-line pt-3">
                <div className="mb-2 text-sm font-semibold">Schedule</div>
                {conns.length === 0 ? <Alert tone="warn">No verified {item.platform} connection. <Link href="/integrations" className="underline">Connect one</Link>.</Alert> : <>
                  <Field label="Account">{(fid) => <Select id={fid} value={sched.connection_id} onChange={(e) => setSched({ ...sched, connection_id: e.target.value })}>{conns.map((c) => <option key={c.id} value={c.id}>{c.account_label || c.name}</option>)}</Select>}</Field>
                  <Field label="When" hint={can.tz}>{(fid) => <Input id={fid} type="datetime-local" value={sched.when} onChange={(e) => setSched({ ...sched, when: e.target.value })} />}</Field>
                  {liveTarget && (
                    <label className="mb-2 flex items-start gap-2 text-sm text-bad">
                      <input type="checkbox" checked={sched.confirm} onChange={(e) => setSched({ ...sched, confirm: e.target.checked })} className="mt-1" />
                      I understand this will publish v{item.version} to a real account at the chosen time.
                    </label>
                  )}
                  {liveTarget && !integ.data?.live_publishing_enabled && <Alert tone="warn">Live publishing is turned off (LIVE_PUBLISHING_ENABLED=false).</Alert>}
                  {liveTarget && !item.publish_authorized && <Alert tone="warn">This version was approved without publish authorisation.</Alert>}
                  <Button variant="primary" className="mt-2" busy={act.busy} disabled={liveTarget && !sched.confirm}
                    onClick={() => doAct(() => api.post<ContentItem>(`/api/content/${id}/schedule`, { connection_id: Number(sched.connection_id), when: sched.when || null, confirm: sched.confirm }))}>Schedule</Button>
                </>}
              </div>
            )}
          </Card>
          {(item.attempts?.length || 0) > 0 && (
            <Card title="Publishing attempts">
              <ul className="space-y-2 text-xs">
                {item.attempts!.map((a) => (
                  <li key={a.id}><div className="flex flex-wrap items-center gap-1.5">{a.success ? <Badge tone="ok">success</Badge> : <Badge tone="bad">failed</Badge>}<Badge tone={a.live ? "bad" : "demo"}>{a.live ? "LIVE" : "simulated"}</Badge><span className="text-muted">{a.adapter} · {fmtDate(a.attempted_at, can.tz)}</span></div>
                    <div className="mt-0.5">{a.summary || a.error}</div>{a.provider_status && <div className="text-muted">Provider status: {a.provider_status}</div>}</li>
                ))}
              </ul>
            </Card>
          )}
          {(item.metrics?.length || 0) > 0 && (
            <Card title="Metrics">
              {(() => { const m = item.metrics![item.metrics!.length - 1]; return (
                <div className="text-sm"><div className="mb-1"><Badge tone={m.source === "synthetic" ? "demo" : "ok"}>{m.source}</Badge> <span className="text-xs text-muted">{fmtDate(m.captured_at, can.tz)}</span></div>
                  <div className="grid grid-cols-3 gap-2">{(["reach", "views", "likes", "comments", "shares", "saves"] as const).map((k) => <div key={k}><div className="text-xs text-muted">{k}</div><div className="font-semibold">{fmtNum(m[k])}</div></div>)}</div></div>
              ); })()}
            </Card>
          )}
        </div>
      </div>

      <Modal open={regenOpen} onClose={() => setRegenOpen(false)} title={isVariant ? `Regenerate the ${item.platform} version` : "Regenerate caption"}>
        <p className="mb-2 text-sm text-muted">Creates a new version of this item only{isVariant ? "; the other platforms aren't touched" : ""}. If it was approved, it will need approval again.{state?.mode === "demo" && " In demo mode the deterministic generator ignores instructions."}</p>
        {!isVariant && <Field label="Instructions (optional)">{(fid) => <Textarea id={fid} value={instructions} onChange={(e) => setInstructions(e.target.value)} placeholder="Shorter, lead with the festival, no emoji" />}</Field>}
        <Button variant="primary" busy={act.busy} onClick={async () => { await doAct(() => api.post<ContentItem>(`/api/content/${id}/regenerate`, { instructions })); setRegenOpen(false); }}>Regenerate</Button>
      </Modal>
      <Modal open={variantsOpen} onClose={() => setVariantsOpen(false)} title="Add platform versions">
        <p className="mb-2 text-sm text-muted">Each platform gets its own adapted caption, a re-rendered image at its size, and its own approval and publishing status.</p>
        <div className="mb-3 grid grid-cols-2 gap-2">
          {PLATFORM_ORDER.filter((p) => !existing.has(p)).map((p) => (
            <label key={p} className="flex items-center gap-2 text-sm"><input type="checkbox" checked={newPlatforms.includes(p)} onChange={(e) => setNewPlatforms(e.target.checked ? [...newPlatforms, p] : newPlatforms.filter((x) => x !== p))} /><PlatformChip platform={p} withLabel /></label>
          ))}
        </div>
        <Button variant="primary" busy={act.busy} disabled={!newPlatforms.length} onClick={async () => { await doAct(() => api.post<{ item: ContentItem }>(`/api/content/${id}/variants`, { platforms: newPlatforms })); setVariantsOpen(false); setNewPlatforms([]); }}>Create {newPlatforms.length || ""} version(s)</Button>
      </Modal>
    </div>
  );
}
