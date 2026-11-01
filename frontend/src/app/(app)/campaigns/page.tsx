"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { useCan } from "@/components/auth";
import { GenerateCampaignButton } from "@/components/campaign-generation";
import { SyntheticBadge } from "@/components/domain";
import { Alert, Badge, Button, Empty, Field, Input, Loading, Modal, PageHeader, Textarea, useAction, useApi } from "@/components/ui";
import { api } from "@/lib/api";
import type { Campaign } from "@/lib/types";

export default function CampaignsPage() {
  const can = useCan();
  const router = useRouter();
  const { data, loading, error } = useApi<Campaign[]>("/api/campaigns");
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState({ name: "", objective: "", audience: "", geography: "", start_date: "", end_date: "" });
  const act = useAction();
  const create = async () => {
    const r = await act.run(() => api.post<Campaign>("/api/campaigns", { ...form, start_date: form.start_date || null, end_date: form.end_date || null }));
    if (r) router.push(`/campaigns/${r.id}`);
  };
  return (
    <div>
      <PageHeader title="Campaigns" subtitle="Track multi-platform campaign progress, content and dates."
        actions={can.edit && <><GenerateCampaignButton /><Link href="/agent" className="rounded-xl bg-accent px-4 py-3 text-sm font-semibold text-accent-ink">Plan with the agent</Link><Button onClick={() => setOpen(true)}>+ New campaign</Button></>} />
      {loading && !data ? <Loading /> : error ? <Alert tone="bad">{error}</Alert> : !data?.length ? <Empty title="No campaigns yet" /> : (
        <div className="grid gap-5 md:grid-cols-2">
          {data.map((c) => {
            const counts = c.counts || {};
            const total = Object.values(counts).reduce((a, b) => a + b, 0);
            return (
              <Link key={c.id} href={`/campaigns/${c.id}`} className="block rounded-3xl border border-line bg-panel p-6 transition hover:border-accent/50">
                <div className="flex flex-wrap items-center gap-2"><span className="text-lg font-semibold">{c.name}</span><Badge tone={c.status === "completed" ? "neutral" : "accent"}>{c.status}</Badge><SyntheticBadge show={c.is_synthetic} /></div>
                <p className="mt-2 min-h-12 line-clamp-2 text-sm leading-6 text-muted">{c.objective}</p>
                <div className="mt-4 flex flex-wrap gap-1.5 text-xs text-muted">
                  <span>{c.start_date || "—"} → {c.end_date || "—"}</span><span>·</span><span>{total} item(s)</span>
                  {counts.in_review ? <Badge tone="warn">{counts.in_review} in review</Badge> : null}
                  {counts.published ? <Badge tone="ok">{counts.published} published</Badge> : null}
                </div>
              </Link>
            );
          })}
        </div>
      )}
      <Modal open={open} onClose={() => setOpen(false)} title="New campaign">
        <Field label="Name">{(id) => <Input id={id} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />}</Field>
        <Field label="Objective">{(id) => <Textarea id={id} value={form.objective} onChange={(e) => setForm({ ...form, objective: e.target.value })} />}</Field>
        <Field label="Audience">{(id) => <Input id={id} value={form.audience} onChange={(e) => setForm({ ...form, audience: e.target.value })} />}</Field>
        <Field label="Geography">{(id) => <Input id={id} value={form.geography} onChange={(e) => setForm({ ...form, geography: e.target.value })} />}</Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Start">{(id) => <Input id={id} type="date" value={form.start_date} onChange={(e) => setForm({ ...form, start_date: e.target.value })} />}</Field>
          <Field label="End">{(id) => <Input id={id} type="date" value={form.end_date} onChange={(e) => setForm({ ...form, end_date: e.target.value })} />}</Field>
        </div>
        {act.error && <Alert tone="bad">{act.error}</Alert>}
        <Button variant="primary" busy={act.busy} onClick={create} disabled={!form.name}>Create</Button>
      </Modal>
    </div>
  );
}
