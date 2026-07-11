export const demoCampaign = {
  brand: "Northstar Cloud",
  campaign: "Signal 2.0 product launch",
  dueDate: "2026-07-24",
  objective: "Create qualified pipeline for the analytics workspace launch.",
  owner: "Maya Chen",
  readiness: {
    completed: 6,
    total: 9,
  },
  workspace: "Northstar — Growth",
} as const;

export const demoLaunchItems = [
  {
    id: "positioning",
    label: "Positioning and message house",
    owner: "Maya Chen",
    status: "complete",
  },
  {
    id: "launch-video",
    label: "Launch video final cut",
    owner: "Eli Brooks",
    status: "at-risk",
  },
  {
    id: "client-approval",
    label: "Executive approval",
    owner: "Priya Raman",
    status: "blocked",
  },
  {
    id: "social-sequence",
    label: "LinkedIn + Instagram sequence",
    owner: "Jordan Lee",
    status: "in-review",
  },
] as const;
