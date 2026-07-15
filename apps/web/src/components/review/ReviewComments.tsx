/** Review-task collaboration thread (REV-011).
 *
 * Comments are independent of correction ownership: anyone who can review
 * the task can leave context for the next reviewer. The global Review Studio
 * `C` shortcut focuses the composer through its stable data attribute.
 */

import { Banner, Button, Skeleton } from "@soa/design-system";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { addReviewComment, fetchReviewComments, type ReviewComment } from "../../api/client";

const MAX_COMMENT_LENGTH = 4_000;

export function ReviewComments({
  organizationSlug,
  taskId,
}: {
  organizationSlug: string;
  taskId: string;
}) {
  const queryClient = useQueryClient();
  const queryKey = ["review-comments", organizationSlug, taskId] as const;
  const [draft, setDraft] = useState("");

  const comments = useQuery({
    queryKey,
    queryFn: () => fetchReviewComments(organizationSlug, taskId),
  });
  const add = useMutation({
    mutationFn: (body: string) => addReviewComment(organizationSlug, taskId, body),
    onSuccess: (comment) => {
      queryClient.setQueryData<{ items: ReviewComment[] }>(queryKey, (current) => ({
        items: [...(current?.items ?? []), comment],
      }));
      setDraft("");
    },
  });

  const body = draft.trim();

  return (
    <section aria-labelledby="review-comments-heading" style={{ display: "grid", gap: "0.75rem" }}>
      <h2 id="review-comments-heading" style={{ font: "var(--soa-font-heading-sm)", margin: 0 }}>
        Discussion
      </h2>

      {comments.status === "pending" ? <Skeleton height="4rem" /> : null}
      {comments.status === "error" ? (
        <Banner
          tone="critical"
          title="Couldn’t load the discussion"
          action={
            <Button size="sm" onPress={() => void comments.refetch()}>
              Try again
            </Button>
          }
        >
          Existing comments are temporarily unavailable. You can still draft a new one.
        </Banner>
      ) : null}
      {comments.status === "success" && comments.data.items.length === 0 ? (
        <p style={{ margin: 0, color: "var(--soa-text-muted)" }}>
          No comments yet. Add context, a question, or an @mention for the next reviewer.
        </p>
      ) : null}
      {comments.status === "success" && comments.data.items.length > 0 ? (
        <ol
          aria-label="Review comments"
          style={{ listStyle: "none", display: "grid", gap: "0.5rem", margin: 0, padding: 0 }}
        >
          {comments.data.items.map((comment) => (
            <li
              key={comment.id}
              style={{
                border: "1px solid var(--soa-border)",
                borderRadius: "var(--soa-radius-panel)",
                padding: "var(--soa-space-3)",
              }}
            >
              <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap" }}>
                <strong>{comment.author}</strong>
                <time dateTime={comment.created_at} style={{ color: "var(--soa-text-muted)" }}>
                  {new Date(comment.created_at).toLocaleString()}
                </time>
              </div>
              <p style={{ marginBlock: "0.25rem 0", whiteSpace: "pre-wrap" }}>{comment.body}</p>
            </li>
          ))}
        </ol>
      ) : null}

      <div style={{ display: "grid", gap: "0.375rem" }}>
        <label htmlFor="review-comment-input">Add comment</label>
        <textarea
          id="review-comment-input"
          data-review-comment-input
          value={draft}
          maxLength={MAX_COMMENT_LENGTH}
          rows={3}
          onChange={(event) => setDraft(event.target.value)}
          aria-describedby="review-comment-description"
          style={{ font: "inherit" }}
        />
        <span id="review-comment-description" style={{ font: "var(--soa-font-caption)" }}>
          {draft.length}/{MAX_COMMENT_LENGTH}. @mentions are recorded with the comment.
        </span>
        {add.isError ? (
          <Banner tone="critical" title="Comment wasn’t added">
            {add.error instanceof Error ? add.error.message : "The service did not respond."}
          </Banner>
        ) : null}
        <div>
          <Button
            size="sm"
            isDisabled={body.length === 0 || add.isPending}
            onPress={() => add.mutate(body)}
          >
            {add.isPending ? "Adding…" : "Add comment"}
          </Button>
        </div>
      </div>
    </section>
  );
}
