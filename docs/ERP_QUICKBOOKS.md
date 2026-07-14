# QuickBooks Online connector (EXP-011)

The first real ERP connector: it delivers a reviewed, mapped sales order
to [QuickBooks Online](https://developer.intuit.com/) via the v3 REST API.
It implements the EXP-010 generic adapter contract, so export
orchestration reaches it exactly like any other destination — no
orchestration change, just a registered adapter and a configurable
`quickbooks_online` integration type.

## The object model: Estimate, not "Sales Order"

QuickBooks Online has **no sales-order object**. For a seller acting on a
customer purchase order, the sales-order equivalent is an **Estimate** (a
non-posting quote); a deployment that prefers a posting document maps to
an **Invoice** instead. The adapter is transport, auth, and response
classification only — the **mapping profile** (EXP-002/003) is what turns
the canonical order into the QuickBooks Estimate/Invoice JSON. Point the
integration's endpoint at the matching resource:

```
https://quickbooks.api.intuit.com/v3/company/{realmId}/estimate
```

(use `sandbox-quickbooks.api.intuit.com` for the developer sandbox).

## Setup

1. Create an app in the Intuit developer portal and connect it to your
   (or the customer's) QuickBooks company to obtain the **realmId**
   (company id) and OAuth2 tokens.
2. Store the OAuth2 **access token** as the integration's credential — it
   is never written to the database as a raw value; only a
   `secretref://…` reference is persisted (SEC-005), and the worker
   resolves it per delivery. Token **refresh** is the credential layer's
   responsibility; the adapter treats a `401` as terminal because
   retrying an expired token cannot succeed.
3. Configure the integration with `integration_type = "quickbooks_online"`
   and the endpoint URL above (the `realmId` is parsed from the path).
4. Author a mapping profile that produces a valid Estimate/Invoice object
   from the canonical order.

## Behavior

- **Auth** — the resolved access token travels as
  `Authorization: Bearer`; it is never logged or echoed.
- **Idempotency** — each delivery carries a `requestid` query parameter
  derived from the export business key, so a retried delivery does not
  create a duplicate document. The pinned `minorversion` keeps the field
  contract stable.
- **Connection test** — a **read-only** `CompanyInfo` GET proves the token
  and company access without creating anything.
- **Result classification** — `2xx` delivered; `429` and `5xx` retryable;
  `401`/`403` (auth) and other `4xx` (validation) terminal.
- **Safe errors** — a QuickBooks `Fault` body can echo submitted content,
  so only the fault **type and code** enter the redacted error; the
  message, detail, and token never do.

## Other ERPs

NetSuite, SAP, and Microsoft Dynamics 365 reuse the same EXP-010 contract
— each is a new adapter + registered integration type, not an
orchestration change. QuickBooks Online is the first because its OAuth2
REST API and JSON object model are the simplest to integrate; the others
follow the pattern established here. See
[`erp_adapter.py`](../apps/worker/src/soa_worker/erp_adapter.py).
