import type { ArtifactSummary, ResultBranchSummary } from "./api";

export interface GlbPreviewBranch {
  name: string;
  status: string;
  metrics: Record<string, number | boolean>;
  artifact: ArtifactSummary;
}

export function selectGlbPreviewBranches(
  branches: ResultBranchSummary[],
): GlbPreviewBranch[] {
  return branches.flatMap((branch) => {
    const artifact = branch.artifacts.find(
      (candidate) => candidate.kind === "glb" && candidate.download_url !== null,
    );
    return artifact
      ? [{
          name: branch.name,
          status: branch.status,
          metrics: branch.metrics,
          artifact,
        }]
      : [];
  });
}
