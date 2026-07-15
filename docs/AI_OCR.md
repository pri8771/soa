# AI, OCR, Evidence, and Evaluation

## 1. Core rule

SOA must use a hybrid document-understanding pipeline. It must not send every page directly to one LLM and treat the response as truth.

**Current launch boundary:** the worker prefers native page text and falls back
to bounded Tesseract OCR, then sends recognized text (not page images) through
the pinned schema-constrained extraction provider. It enforces one sales order
per input. Layout-aware/multimodal extraction, managed OCR, classification, and
packet splitting remain target capabilities that require separate corpus
evaluation.

The following is the target hybrid pipeline; stages that are outside the
current launch boundary above stay disabled until implemented and evaluated:

```text
File inspection
→ Native text extraction where reliable
→ Image rendering and preprocessing where needed
→ OCR and layout extraction
→ Classification and packet splitting
→ Schema-constrained extraction
→ Deterministic normalization
→ Reference-data matching
→ Business-rule validation
→ Confidence calibration
→ Auto-approval or field-level review
```

## 2. Provider separation

Define separate provider contracts for:

- Native text extraction
- OCR
- Layout and table extraction
- Classification
- Document splitting
- Structured extraction
- Embeddings/fuzzy semantic matching
- Optional validation assistance

Do not collapse these responsibilities into one provider-specific service.

## 3. Target free-first processing policy

### Digital PDF

1. Extract embedded text and character positions.
2. Measure text coverage and quality.
3. Render pages only when visual evidence or table structure is required.
4. Run schema extraction using the text and selected page images.

### Scanned document

1. Render pages at a bounded resolution.
2. Detect orientation, skew, noise, blank pages, and poor image quality.
3. Apply local preprocessing.
4. Run open-source OCR and layout extraction.
5. Escalate to a managed OCR provider only when configured quality rules fail.

### Difficult document

1. Run the normal path.
2. Detect low-quality OCR, schema failure, model disagreement, or validation failure.
3. Retry with bounded repair logic.
4. Use a configured fallback provider when policy permits.
5. Route only unresolved fields to review.

## 4. Structured extraction contract

Every extraction provider returns a typed response matching the published schema. The system must reject or repair invalid structures before they reach business logic.

Each field result contains:

```text
field_path
raw_value
normalized_value
page_number
bounding_box / polygon
source_text
source_artifact_id
extraction_method
provider
provider_model
provider_request_id
prompt_or_instruction_version
schema_version
provider_confidence (when available)
platform_confidence
validation_status
candidate_values
```

Line-item cells receive the same evidence structure as header fields.

## 5. Evidence-first requirement

No extracted value may be auto-approved unless the platform retains sufficient evidence to explain its origin.

The Review Studio must support:

- Clicking a field to highlight its source
- Clicking source text to populate or replace a field
- Displaying original and normalized values
- Showing alternative candidates
- Showing catalog-match features and scores
- Explaining which validations passed or failed
- Showing which provider/configuration produced the result

When precise coordinates are unavailable, the result must be labeled as text-level or page-level evidence rather than pretending coordinates exist.

## 6. Prompt-injection and untrusted content

Document text is untrusted data, never an instruction source.

Controls:

- System and developer instructions are separated from document content.
- Extraction models have no web access, arbitrary tool use, or credential access.
- Document content is enclosed and labeled as data.
- Outputs are limited to an explicit schema.
- URLs, commands, and instructions found inside documents are never executed.
- Outputs are validated, length-bounded, type-checked, and sanitized.
- Cross-document and cross-tenant context is forbidden.
- Provider requests contain only the minimum necessary data.

## 7. Normalization

Normalization is deterministic code wherever possible:

- Dates and date ranges
- Decimal separators
- Currency symbols and ISO codes
- Thousand separators
- Units of measure
- Addresses and postal codes
- Phone and email formats
- Customer and material identifiers
- Boolean/enumerated values
- Whitespace and OCR character substitutions

The raw extracted value is always retained beside the normalized value.

## 8. Reference-data matching

Reference matching may use exact, normalized, fuzzy, weighted, and semantic signals, but the final match must be explainable.

Example Ship-To score:

```text
Company name similarity       20%
Address-line similarity       25%
City match                    15%
Postal-code match             30%
Country match                 10%
```

Weights are versioned and configurable by process or stream. Candidate lists, individual feature scores, selected record, override reason, and reviewer identity are retained.

## 9. Validation

### Field rules

- Required/optional
- Type and format
- Allowed values
- Regex and length
- Minimum/maximum
- Date window
- Decimal precision

### Cross-field rules

- Header total reconciles to line totals, tax, discount, and freight
- Quantity × unit price reconciles to line total
- Requested date is not before PO date
- Currency is consistent across header and lines
- Ship-To belongs to the selected customer
- Material is valid for the customer and effective date
- Quantity respects package or unit constraints
- PO number is not already accepted for the same customer

### External rules

- ERP customer/material existence
- Contract price
- Credit or order-hold status where authorized
- Address validation
- Customer-specific policies

LLMs may propose rules in natural language, but published rules must compile to inspectable deterministic logic.

## 10. Confidence model

Provider confidence alone is insufficient. Platform confidence combines evidence such as:

- OCR/text quality
- Direct evidence availability
- Model/provider agreement
- Schema validity
- Rule outcomes
- Reference-match strength
- Historical accuracy for comparable documents
- Document quality and ambiguity
- Human correction history

Confidence is calibrated against actual correctness. Thresholds are field-specific and risk-specific; critical fields require stricter policy than informational fields.

## 11. Field criticality

### Critical

- PO number
- Customer/sold-to
- Ship-to
- Material
- Quantity
- Unit of measure
- Unit price where used for posting
- Currency

### Important

- Dates
- Payment terms
- Incoterms
- Discounts, tax, freight, and totals

### Informational

- Free-text notes
- Contact details
- Non-posting annotations

Auto-approval policy must be based on risk and criticality, not only overall document confidence.

## 12. Golden datasets

The repository contains a versioned synthetic cohort/expected-result manifest,
but not rights-cleared representative source bytes. That manifest exercises the
evaluation machinery; it is not evidence of production accuracy. The pilot
gate must populate and reconcile the source corpus below.

Maintain representative labeled documents across:

- Customers and suppliers
- Countries and languages
- Native and scanned PDFs
- Good and poor image quality
- Single and multi-page documents
- Combined packets
- Different table layouts
- Multi-line descriptions
- Continuation pages
- Handwritten marks and stamps
- Missing or conflicting values

Ground truth includes document class, split boundaries, normalized fields, line-item cells, and expected validation outcomes.

## 13. Evaluation metrics

- Classification precision/recall
- Split-boundary accuracy
- Exact and normalized field accuracy
- Critical-field accuracy
- Line detection and cell accuracy
- Table precision/recall
- Reference-match accuracy
- Validation precision/recall
- False auto-approval rate
- Review rate
- Average corrections per document
- Latency and cost per document

## 14. Promotion workflow

Every model, prompt, schema, matching policy, or rule change must:

1. Run against the applicable gold datasets.
2. Compare to the current production version.
3. Report improvements and regressions by field and cohort.
4. Pass critical-field and false-auto-approval gates.
5. Run in shadow mode where practical.
6. Be published as a versioned configuration.
7. Support immediate rollback.

Human corrections are evaluation and improvement inputs. They do not automatically alter production behavior.
