export type SocialNetwork = "instagram" | "linkedin" | "mock";

export interface SocialCapabilities {
  readonly analytics: boolean;
  readonly imagePosts: boolean;
  readonly network: SocialNetwork;
  readonly textPosts: boolean;
  readonly videoPosts: boolean;
}

export interface SocialConnector {
  readonly capabilities: SocialCapabilities;
  readonly id: string;
  readonly displayName: string;
}

export const mockSocialConnector: SocialConnector = {
  capabilities: {
    analytics: true,
    imagePosts: true,
    network: "mock",
    textPosts: true,
    videoPosts: true,
  },
  displayName: "Local social simulator",
  id: "mock-local",
};
