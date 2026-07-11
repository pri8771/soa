export interface CampaignHealthInput {
  readonly blocked: number;
  readonly dueSoon: number;
  readonly status: "on-track" | "at-risk" | "blocked";
}

export interface PortfolioHealthSummary {
  readonly blockedItems: number;
  readonly dueSoonItems: number;
  readonly onTrackRate: number;
}

export function summarizePortfolioHealth(
  campaigns: readonly CampaignHealthInput[],
): PortfolioHealthSummary {
  if (campaigns.length === 0) {
    return {
      blockedItems: 0,
      dueSoonItems: 0,
      onTrackRate: 100,
    };
  }

  const onTrack = campaigns.filter((campaign) => campaign.status === "on-track").length;
  return {
    blockedItems: campaigns.reduce((total, campaign) => total + campaign.blocked, 0),
    dueSoonItems: campaigns.reduce((total, campaign) => total + campaign.dueSoon, 0),
    onTrackRate: Math.round((onTrack / campaigns.length) * 100),
  };
}
