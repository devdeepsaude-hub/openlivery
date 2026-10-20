"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { Lang } from "@/lib/i18n";
import type { Industry, IndustryLabel } from "@/types";

// The industry catalog comes from the API (one source of truth for codes and
// labels in both languages). It is small and static, so one fetch per page
// load is shared by every component on the page.
let cached: Industry[] | null = null;
let pending: Promise<Industry[]> | null = null;

export function loadIndustries(): Promise<Industry[]> {
  if (cached) return Promise.resolve(cached);
  if (!pending) pending = api<Industry[]>("/industries").then((items) => { cached = items; return items; }).finally(() => { pending = null; });
  return pending;
}

export function useIndustries(): Industry[] {
  const [items, setItems] = useState<Industry[]>(cached || []);
  useEffect(() => { if (!cached) loadIndustries().then(setItems).catch(() => {}); }, []);
  return items;
}

// The catalog is labelled in English and Spanish only; Portuguese reads
// closest to Spanish, as the API does when it describes the business.
export function industryLabel(label: IndustryLabel, lang: Lang): string {
  return label[lang === "pt" ? "es" : lang];
}

// Words for a client: the business type when known, else the client's own
// words, else the industry. "other" carries no information and is skipped.
export function businessLabel(catalog: Industry[], client: { industry: string; business_type: string; business_custom: string }, lang: Lang): string {
  const sector = catalog.find((item) => item.code === client.industry);
  const kind = sector?.types.find((item) => item.code === client.business_type);
  if (kind && kind.code !== "other") return industryLabel(kind.label, lang);
  if (client.business_custom.trim()) return client.business_custom.trim();
  return !sector || sector.code === "other" ? "" : industryLabel(sector.label, lang);
}
