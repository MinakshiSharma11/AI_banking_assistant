import type { Cell } from "./api";

const ARABIC = /[؀-ۿ]/;

/** Text direction from content, so Arabic renders right to left. */
export const dirOf = (text: string | undefined): "rtl" | "ltr" =>
  text && ARABIC.test(text) ? "rtl" : "ltr";

export function formatValue(v: Cell, unit = ""): string {
  if (v === null || v === undefined) return "–";
  if (typeof v === "number") {
    if (unit === "ratio") return `${(v * 100).toFixed(1)}%`;
    if (unit === "KWD") return v.toLocaleString("en-US", { minimumFractionDigits: 3, maximumFractionDigits: 3 });
    if (Number.isInteger(v)) return v.toLocaleString("en-US");
    return v.toLocaleString("en-US", { maximumFractionDigits: 2 });
  }
  if (typeof v === "boolean") return v ? "Yes" : "No";
  return v;
}

export const titleCase = (s: string): string =>
  s.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());

export const ROLE_LABEL: Record<string, string> = {
  branch_manager: "Branch manager",
  regional_head: "Regional head",
  head_office: "Head office",
  it_support: "IT support (live data only)",
};
