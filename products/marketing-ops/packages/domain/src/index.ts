export type CampaignStatus = "draft" | "active" | "at-risk" | "completed";
export type ReadinessState = "blocked" | "at-risk" | "ready";

export interface ReadinessRequirement {
  readonly id: string;
  readonly label: string;
  readonly complete: boolean;
  readonly blocking: boolean;
}

export interface CampaignReadiness {
  readonly completed: number;
  readonly state: ReadinessState;
  readonly total: number;
  readonly unresolved: readonly ReadinessRequirement[];
}

export function calculateCampaignReadiness(
  requirements: readonly ReadinessRequirement[],
): CampaignReadiness {
  const unresolved = requirements.filter((requirement) => !requirement.complete);
  const blocking = unresolved.some((requirement) => requirement.blocking);

  return {
    completed: requirements.length - unresolved.length,
    state: blocking ? "blocked" : unresolved.length > 0 ? "at-risk" : "ready",
    total: requirements.length,
    unresolved,
  };
}
