// GENERATED from the canonical order schema — DO NOT EDIT.
// Source: packages/canonical/src/soa_canonical/schemas/ (version 1.0.0)
// Regenerate: uv run python -m soa_canonical.generate
// The drift test (packages/canonical/tests/test_generation.py) fails CI on edits.

export interface Money {
  amount: string;
  currency: string;
}

export interface ExternalReference {
  scheme: string;
  value: string;
}

export interface Address {
  line1?: string | null;
  line2?: string | null;
  city?: string | null;
  region?: string | null;
  postal_code?: string | null;
  country?: string | null;
}

export interface PartyContact {
  email?: string | null;
  phone?: string | null;
}

export interface Party {
  name: string;
  address?: Address | null;
  contact?: PartyContact | null;
  identifiers?: ExternalReference[];
}

export interface LineItem {
  line_number: number;
  sku?: string | null;
  description?: string | null;
  quantity: string;
  unit_of_measure?: string | null;
  unit_price?: Money | null;
  line_total: Money;
  requested_delivery_date?: string | null;
  notes?: Note[];
}

export interface Note {
  text: string;
  author?: string | null;
  visibility: "internal" | "external";
}

export interface ProvenanceEntry {
  origin: "extracted" | "corrected" | "derived";
  page_number?: number | null;
  quote?: string | null;
  actor?: string | null;
}

export interface CanonicalOrderIdentifiers {
  po_number: string;
  sales_order_reference?: string | null;
  external_references?: ExternalReference[];
}

export interface CanonicalOrderParties {
  buyer?: Party | null;
  seller?: Party | null;
  ship_to?: Party | null;
  bill_to?: Party | null;
}

export interface CanonicalOrderDates {
  order_date: string;
  requested_delivery_date?: string | null;
  promised_delivery_date?: string | null;
}

export interface CanonicalOrderTermsIncoterms {
  code: string;
  location?: string | null;
}

export interface CanonicalOrderTerms {
  currency: string;
  payment_terms?: string | null;
  incoterms?: CanonicalOrderTermsIncoterms | null;
}

export interface CanonicalOrderTotals {
  subtotal?: Money | null;
  discount_total?: Money | null;
  tax_total?: Money | null;
  shipping_total?: Money | null;
  grand_total: Money;
}

export interface CanonicalOrderSource {
  document_id: string;
  run_id: string;
  document_sha256?: string | null;
  received_at?: string | null;
}

export interface CanonicalOrder {
  schema_version: "1.0.0";
  identifiers: CanonicalOrderIdentifiers;
  parties?: CanonicalOrderParties;
  dates: CanonicalOrderDates;
  terms: CanonicalOrderTerms;
  totals: CanonicalOrderTotals;
  line_items: LineItem[];
  notes?: Note[];
  source: CanonicalOrderSource;
  provenance?: Record<string, ProvenanceEntry>;
  extensions?: Record<string, unknown>;
}
