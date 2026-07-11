export interface GenerationRequest {
  readonly instruction: string;
  readonly sourceText: string;
}

export interface GenerationCandidate {
  readonly content: string;
  readonly provider: string;
  readonly provenance: string;
}

export interface AiProvider {
  readonly id: string;
  generate(request: GenerationRequest): Promise<GenerationCandidate>;
}

export class DeterministicMockAiProvider implements AiProvider {
  public readonly id = "mock-deterministic";

  public async generate(request: GenerationRequest): Promise<GenerationCandidate> {
    const normalized = request.sourceText.trim().replace(/\s+/g, " ");
    return {
      content: `${request.instruction.trim()}: ${normalized}`,
      provider: this.id,
      provenance: "local deterministic fixture",
    };
  }
}
