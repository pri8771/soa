# AI Strategy — Local LLM, Gemini, and Future Providers

## 1. Product position

AI should reduce blank-page work, repetitive adaptation, coordination overhead, and analysis time. It must not become an opaque autopilot that publishes unreviewed brand communications, invents performance conclusions, or bypasses approval.

Initial provider support:

- Local models through an Ollama-compatible API
- Gemini

Prepared provider interfaces:

- OpenAI
- Anthropic
- Azure OpenAI
- Other hosted or self-managed providers

## 2. Core rules

1. AI output is a candidate, not canonical truth.
2. No model may publish, approve, change permissions, spend budget, or contact external users directly.
3. Consequential actions are performed by deterministic services after permission and policy checks.
4. Brand, campaign, and audience context is explicit and inspectable.
5. Prompt/instruction versions and provider/model provenance are stored.
6. Customer data is isolated by organization.
7. One tenant’s content, corrections, examples, or embeddings are never used for another tenant.
8. Provider use respects customer data-use, retention, region, and local-only settings.
9. Structured workflows require schema-validated output.
10. Performance insights cite metrics and distinguish facts from inference.

## 3. AI capability groups

### Planning assistance

- Draft campaign brief from intake request
- Identify missing brief questions
- Suggest objectives and measurable outcomes
- Suggest audiences, messages, channels, deliverables, milestones, dependencies, and risks
- Generate a task plan from an approved template and brief
- Summarize campaign status and blockers

### Content assistance

- Ideate content themes
- Draft master content
- Create channel variants
- Rewrite for length, tone, audience, or reading level
- Repurpose long-form material into posts
- Generate headline, CTA, alt-text, and metadata candidates
- Translate with review
- Detect possible prohibited or required language
- Compare a draft to brand rules

### Workflow assistance

- Summarize comments and decisions
- Suggest owners or due dates based on configured rules and workload data
- Convert meeting notes or feedback into draft tasks
- Explain why launch readiness is blocked
- Suggest automation rules without publishing them

### Analytics assistance

- Summarize measured performance
- Identify anomalies and cohort differences
- Suggest hypotheses and experiments
- Create a draft follow-up work item
- Explain metric definitions and missing data

### Engagement assistance

- Draft replies
- Classify intent or urgency
- Suggest escalation
- Summarize conversation history

Replies remain drafts and follow reply-approval policy.

## 4. Provider contracts

```ts
interface AIProvider {
  provider: AIProviderName;
  listModels(): Promise<AIModelDescriptor[]>;
  healthCheck(): Promise<ProviderHealth>;
  generateText(request: TextGenerationRequest): Promise<TextGenerationResult>;
  generateStructured<T>(request: StructuredGenerationRequest<T>): Promise<StructuredGenerationResult<T>>;
  embed?(request: EmbeddingRequest): Promise<EmbeddingResult>;
}
```

Provider result contains:

- Provider and model
- Request/provider identifier
- Created time
- Latency
- Input/output token or equivalent usage when available
- Estimated or actual cost
- Finish reason
- Safety status
- Raw result artifact reference according to policy
- Parsed output

Business services must never import a provider SDK directly.

## 5. Provider policy

AI policy can be configured by organization and narrowed by workspace/brand:

- Enabled providers
- Default model by capability
- Local-only mode
- Data-residency requirement
- Maximum cost per generation
- Maximum input/output size
- Prohibited data classes
- Retention mode
- Whether brand assets may be sent
- Whether analytics data may be sent
- Whether generated content may be stored
- Required approval policy

Provider selection considers:

- Customer policy
- Capability
- Language
- Context size
- Latency
- Cost
- Quality score
- Availability
- Data sensitivity

## 6. Local model strategy

### Local adapter

Use an Ollama-compatible HTTP interface so local development and privacy-sensitive deployments can select installed models without changing application code.

### Local development modes

```text
AI_MODE=mock
AI_MODE=ollama
AI_MODE=gemini
AI_MODE=router
```

`mock` returns deterministic fixtures for automated tests.

### Local use cases

Good initial candidates:

- Short-form rewriting
- Summarization
- Classification
- Tag suggestions
- Structured task suggestions
- Simple channel adaptation
- Local development fixtures

More complex brand reasoning or long-context repurposing must be benchmarked rather than assumed to work locally.

### Operational requirements

- Health and model availability check
- Configurable endpoint
- Request timeout
- Concurrency limit
- Model warm/cold state telemetry
- Input/output bounds
- No assumption that every developer has a GPU
- Graceful fallback to mock or configured hosted provider

## 7. Gemini strategy

Gemini is the initial hosted provider. It must be integrated through the common interface and not used as a global singleton embedded throughout the application.

Requirements:

- Server-side credentials
- Organization/provider policy check before every request
- Structured output where supported
- Safety/result handling
- Bounded retries
- Usage and cost capture
- Provider request identifiers
- Model name stored exactly
- Separate development and production credentials
- Customer-data terms verified before production use

## 8. Future OpenAI and Anthropic integration

The internal contracts, generation records, prompts, evaluations, and policy model must allow these adapters without domain changes.

Expected work:

- Credential and region configuration
- Model/capability registry
- Structured-output mapping
- Streaming mapping
- Safety/error mapping
- Token/cost normalization
- Provider-specific retention configuration
- Contract and evaluation tests

The user interface should present capabilities and approved models, not provider-specific implementation jargon.

## 9. Context assembly

AI context is built by a deterministic context service.

Possible sections:

- Organization policy
- Workspace/client context
- Brand voice and rules
- Audience
- Product/offer
- Campaign brief
- Content source
- Channel requirements
- User instruction
- Relevant approved examples
- Prohibited content and compliance rules
- Requested output schema

Every section has:

- Source object and version
- Sensitivity classification
- Inclusion reason
- Character/token estimate
- Priority

Context assembly logs metadata, not unnecessary raw content, to operational telemetry.

## 10. Brand context

Brand context is structured rather than one long prompt:

- Voice traits
- Tone range
- Do and avoid examples
- Preferred/prohibited terms
- Audience vocabulary
- Product facts
- Required claims
- Prohibited claims
- Legal disclaimers
- Channel-specific rules
- Locale-specific rules
- Approved content examples

Only published brand versions are used by default. Draft brand changes require explicit preview mode.

## 11. Prompt and instruction management

### AIInstruction

- Capability
- System/developer instruction template
- Input schema
- Output schema
- Model requirements
- Safety rules
- Evaluation dataset
- Status

### Versioning

Every change creates an immutable version. Each generation references:

- Instruction version
- Context versions
- Provider/model
- Parameters
- Output schema version

Promotion states:

```text
draft
testing
approved
active
deprecated
```

## 12. Structured output

Use structured generation for:

- Campaign-plan suggestions
- Task/dependency suggestions
- Channel variants
- Content checks
- Analytics summaries and cited findings
- Automation-rule suggestions

Output validation:

1. Parse JSON or provider structured response.
2. Validate against exact schema.
3. Reject unknown unsafe actions.
4. Enforce size/array bounds.
5. Sanitize text for rendering.
6. Retry once with bounded repair when policy allows.
7. Fall back or return inspectable failure.

Invalid output never becomes a persisted business object without explicit user confirmation and server validation.

## 13. Generation workflow

1. User invokes an AI action.
2. API authorizes capability and object access.
3. Policy service selects allowed providers/models.
4. Context service assembles versioned context.
5. Safety and sensitivity checks run.
6. Durable AI job is created for non-trivial work.
7. Provider adapter executes.
8. Output is validated.
9. Candidate is stored.
10. User previews diff and accepts all, accepts parts, edits, or rejects.
11. Accepted content creates a normal human-visible version with AI provenance.
12. Audit and usage records are written.

## 14. UI behavior

AI appears as contextual actions, not a mandatory chatbot.

Required presentation:

- Action name
- Provider/model when policy allows display
- Context summary
- Expected scope of change
- Progress
- Candidate/diff
- Warnings
- Accept/reject controls
- Provenance after acceptance

For multi-candidate generation, users compare candidates side by side or in a compact stack.

AI does not silently overwrite active edits.

## 15. Safety and content risk

Checks include:

- Prompt injection in imported/source content
- Requests for disallowed external actions
- Leakage of private/internal notes into client-facing copy
- Unsupported factual claims
- Prohibited/regulated claims
- Unapproved personal data
- Hidden instructions from external source documents
- Malicious URLs or commands

Source content is labeled as untrusted data. Models have no arbitrary tool or network access through extraction/generation jobs.

## 16. Fact and claim handling

AI may use only:

- User-provided facts
- Published brand/product records
- Explicitly connected approved knowledge sources
- Metrics retrieved by the application

Generated factual claims should include source references when the workflow requires factual precision. The system must not imply that an LLM validated a legal, medical, financial, or product claim.

## 17. Analytics insight contract

An insight result contains:

- Date range
- Objects/accounts included
- Data freshness
- Metrics used
- Factual observations
- Inferences/hypotheses
- Missing data
- Recommended actions
- Confidence/limitations

Factual observations link to underlying records. Causation is never asserted from correlation alone.

## 18. Evaluation program

Datasets:

- Brand alignment examples
- Channel adaptation examples
- Task-plan examples
- Content-policy cases
- Analytics summary cases
- Multilingual cases
- Prompt-injection and leakage cases

Metrics:

- Schema-valid rate
- Human acceptance rate
- Edit distance from accepted version
- Brand-policy precision/recall
- Required-fact preservation
- Prohibited-claim rate
- Cross-tenant leakage rate
- Latency
- Cost
- Local-versus-hosted quality comparison

## 19. Promotion and regression

Before changing an active instruction/model:

1. Run applicable evaluation datasets.
2. Compare active and candidate versions.
3. Review regressions by brand, language, channel, and content type.
4. Pass safety and leakage tests.
5. Run shadow or limited rollout where practical.
6. Publish a version with rollback pointer.
7. Monitor acceptance, error, latency, and cost.

## 20. Data retention

Generation inputs and raw provider responses follow organization policy.

Options:

- Store complete generation evidence
- Store redacted evidence
- Store metadata and accepted result only
- Local-only processing

Deletion must cover provider artifacts under contract where possible, application records, object artifacts, caches, and evaluation copies according to policy.

## 21. Cost controls

- Usage recorded per organization, workspace, brand, campaign, user, capability, provider, and model
- Input/output bounds
- Per-generation and monthly budgets
- Rate limits
- Local-first routing when quality policy allows
- Caching only for deterministic tenant-safe requests
- No unbounded agent loops
- No automatic repeated generation on every keystroke
- Explicit consent before expensive batch generation

## 22. Prohibited shortcuts

- No direct LLM call from the browser
- No provider keys in client code
- No provider-specific fields in core domain records
- No automatic publishing from generated text
- No automatic acceptance of AI policy checks
- No cross-tenant retrieval
- No hidden training use of customer data
- No fabricated citations or metrics
- No unbounded autonomous agents
- No claims that a local model matches hosted quality without evaluation

## 23. P0 acceptance scenario

A user can run the product locally with deterministic AI fixtures or Ollama, draft a campaign brief, generate a task-plan candidate, create platform-specific content variants, compare and partially accept suggestions, retain provider/instruction/context provenance, and complete the campaign workflow without sending data externally. The same feature can be configured to use Gemini through the provider adapter, with policy, usage, error, and evaluation records. OpenAI and Anthropic can later be added by implementing the common contract and passing the same tests.