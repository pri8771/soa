import {
  Badge,
  Banner,
  Button,
  Checkbox,
  Dialog,
  DialogTrigger,
  Skeleton,
  TextField,
} from "@soa/design-system";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import {
  ApiError,
  assignOrganizationRole,
  cancelOrganizationDataExport,
  changeOrganizationMemberStatus,
  createServiceCredential,
  createOrganizationDataExport,
  createOrganizationRole,
  fetchOrganizationDataExports,
  fetchOrganizationMembers,
  fetchOrganizationRoles,
  fetchServiceCredentials,
  fetchStreams,
  inviteOrganizationMember,
  revokeServiceCredential,
  revokeOrganizationRole,
  rotateServiceCredential,
  type DataExportJob,
  type OrganizationMember,
  type ServiceCredentialEntry,
  type ServiceCredentialSecretResponse,
} from "../api/client";
import { AppShell } from "../shell/AppShell";
import { useShellSession } from "../shell/ShellContext";

function failure(error: unknown, fallback: string): React.ReactNode {
  if (!(error instanceof ApiError)) return error instanceof Error ? error.message : fallback;
  return (
    <>
      {error.message}
      {error.correlationId ? (
        <span>
          {" "}
          Support reference: <code>{error.correlationId}</code>.
        </span>
      ) : null}
    </>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section
      aria-label={title}
      style={{
        border: "1px solid var(--soa-border)",
        borderRadius: "var(--soa-radius-panel)",
        background: "var(--soa-surface)",
        padding: "var(--soa-space-5)",
        display: "grid",
        gap: "var(--soa-space-4)",
      }}
    >
      <h2 style={{ margin: 0, font: "var(--soa-font-heading-md)" }}>{title}</h2>
      {children}
    </section>
  );
}

function MemberStatusDialog({
  member,
  target,
  onConfirm,
}: {
  member: OrganizationMember;
  target: "active" | "suspended" | "removed";
  onConfirm: () => void;
}) {
  const permanent = target === "removed";
  return (
    <Dialog title={`${target === "active" ? "Reactivate" : target} member?`} alert={permanent}>
      {({ close }) => (
        <div style={{ display: "grid", gap: "var(--soa-space-4)" }}>
          <p style={{ margin: 0 }}>
            {target === "active"
              ? `${member.invited_email} will regain access from their assigned roles.`
              : target === "suspended"
                ? `${member.invited_email} will immediately lose organization access until reactivated.`
                : `${member.invited_email} will be removed permanently. Removed memberships cannot be reactivated.`}
          </p>
          <div style={{ display: "flex", justifyContent: "flex-end", gap: "var(--soa-space-2)" }}>
            <Button variant="subtle" onPress={close}>
              Keep current status
            </Button>
            <Button
              variant={permanent ? "destructive" : "primary"}
              onPress={() => {
                onConfirm();
                close();
              }}
            >
              Confirm {target}
            </Button>
          </div>
        </div>
      )}
    </Dialog>
  );
}

function MemberRoleAssignments({
  organizationSlug,
  member,
  canReadRoles,
  canManageRoles,
}: {
  organizationSlug: string;
  member: OrganizationMember;
  canReadRoles: boolean;
  canManageRoles: boolean;
}) {
  const queryClient = useQueryClient();
  const revoke = useMutation({
    mutationFn: (roleSlug: string) =>
      revokeOrganizationRole(organizationSlug, member.membership_id, roleSlug),
    onSuccess: () =>
      queryClient.invalidateQueries({
        queryKey: ["organization-members", organizationSlug],
      }),
  });

  if (!canReadRoles) return null;
  return (
    <>
      {member.assigned_roles.length === 0 ? (
        <span style={{ color: "var(--soa-text-muted)" }}>no roles</span>
      ) : (
        member.assigned_roles.map((role) => (
          <span
            key={role.id}
            style={{ display: "inline-flex", alignItems: "center", gap: "var(--soa-space-1)" }}
          >
            <Badge tone={role.slug === "org-admin" ? "info" : "neutral"}>{role.name}</Badge>
            {canManageRoles ? (
              <DialogTrigger>
                <Button size="sm" variant="subtle">
                  Revoke {role.name}
                </Button>
                <Dialog title={`Revoke ${role.name}?`} alert={role.slug === "org-admin"}>
                  {({ close }) => (
                    <div style={{ display: "grid", gap: "var(--soa-space-4)" }}>
                      <p style={{ margin: 0 }}>
                        {member.invited_email} will immediately lose permissions granted only by
                        this role. The server will refuse removal of the last active organization
                        admin.
                      </p>
                      <div
                        style={{
                          display: "flex",
                          justifyContent: "flex-end",
                          gap: "var(--soa-space-2)",
                        }}
                      >
                        <Button variant="subtle" onPress={close}>
                          Keep role
                        </Button>
                        <Button
                          variant="destructive"
                          isDisabled={revoke.isPending}
                          onPress={() => revoke.mutate(role.slug, { onSuccess: close })}
                        >
                          Revoke role
                        </Button>
                      </div>
                    </div>
                  )}
                </Dialog>
              </DialogTrigger>
            ) : null}
          </span>
        ))
      )}
      {revoke.isError ? (
        <Banner tone="critical" title="Role wasn’t revoked">
          {failure(revoke.error, "The role assignment was not changed.")}
        </Banner>
      ) : null}
    </>
  );
}

function MembersAndRoles({ organizationSlug }: { organizationSlug: string }) {
  const session = useShellSession();
  const queryClient = useQueryClient();
  const canReadMembers = session.permissions.has("members.read");
  const canManageMembers = session.permissions.has("members.manage");
  const canReadRoles = session.permissions.has("roles.read");
  const canManageRoles = session.permissions.has("roles.manage");
  const [email, setEmail] = useState("");
  const [roleName, setRoleName] = useState("");
  const [roleSlug, setRoleSlug] = useState("");
  const [rolePermissions, setRolePermissions] = useState("");
  const [assignments, setAssignments] = useState<Record<string, string>>({});

  const members = useQuery({
    queryKey: ["organization-members", organizationSlug],
    queryFn: () => fetchOrganizationMembers(organizationSlug),
    enabled: canReadMembers,
  });
  const roles = useQuery({
    queryKey: ["organization-roles", organizationSlug],
    queryFn: () => fetchOrganizationRoles(organizationSlug),
    enabled: canReadRoles,
  });
  const invite = useMutation({
    mutationFn: () => inviteOrganizationMember(organizationSlug, email.trim()),
    onSuccess: () => {
      setEmail("");
      void queryClient.invalidateQueries({ queryKey: ["organization-members", organizationSlug] });
    },
  });
  const statusChange = useMutation({
    mutationFn: (input: {
      member: OrganizationMember;
      status: "active" | "suspended" | "removed";
    }) => changeOrganizationMemberStatus(organizationSlug, input.member, input.status),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["organization-members", organizationSlug] }),
  });
  const createRole = useMutation({
    mutationFn: () =>
      createOrganizationRole(organizationSlug, {
        name: roleName.trim(),
        slug: roleSlug.trim(),
        permissions: rolePermissions
          .split(",")
          .map((value) => value.trim())
          .filter(Boolean),
      }),
    onSuccess: () => {
      setRoleName("");
      setRoleSlug("");
      setRolePermissions("");
      void queryClient.invalidateQueries({ queryKey: ["organization-roles", organizationSlug] });
    },
  });
  const assignRole = useMutation({
    mutationFn: (membershipId: string) =>
      assignOrganizationRole(organizationSlug, membershipId, assignments[membershipId] ?? ""),
    onSuccess: (_result, membershipId) => {
      setAssignments((current) => ({ ...current, [membershipId]: "" }));
      void queryClient.invalidateQueries({
        queryKey: ["organization-members", organizationSlug],
      });
    },
  });

  if (!canReadMembers && !canReadRoles) {
    return (
      <Section title="Members and roles">
        <Banner tone="info" title="Additional permission required">
          Viewing this area requires members.read or roles.read.
        </Banner>
      </Section>
    );
  }

  return (
    <Section title="Members and roles">
      {invite.isError ? (
        <Banner tone="critical" title="Invitation failed">
          {failure(invite.error, "No invitation was created.")}
        </Banner>
      ) : null}
      {invite.isSuccess ? (
        <Banner
          tone="success"
          title={invite.data.created ? "Invitation created" : "Invitation refreshed"}
        >
          The invitation record for {invite.data.email} is ready. This app does not send an email
          yet; share the organization slug and sign-in instructions through an approved channel.
        </Banner>
      ) : null}
      {canManageMembers ? (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            invite.mutate();
          }}
          style={{
            display: "flex",
            alignItems: "end",
            gap: "var(--soa-space-3)",
            flexWrap: "wrap",
          }}
        >
          <TextField
            label="Invite by email"
            type="email"
            value={email}
            onChange={setEmail}
            isRequired
          />
          <Button type="submit" isDisabled={!email.includes("@") || invite.isPending}>
            Create invitation
          </Button>
        </form>
      ) : null}

      {members.status === "pending" ? <Skeleton height="8rem" /> : null}
      {members.status === "error" ? (
        <Banner tone="critical" title="Couldn’t load members">
          {failure(members.error, "The member directory is unavailable.")}
        </Banner>
      ) : null}
      {members.data ? (
        <ul
          style={{
            listStyle: "none",
            margin: 0,
            padding: 0,
            display: "grid",
            gap: "var(--soa-space-3)",
          }}
        >
          {members.data.items.map((member) => (
            <li
              key={member.membership_id}
              style={{
                display: "flex",
                alignItems: "center",
                flexWrap: "wrap",
                gap: "var(--soa-space-3)",
                paddingBottom: "var(--soa-space-3)",
                borderBottom: "1px solid var(--soa-border)",
              }}
            >
              <strong>{member.invited_email}</strong>
              <Badge
                tone={
                  member.status === "active"
                    ? "success"
                    : member.status === "removed"
                      ? "critical"
                      : "warning"
                }
              >
                {member.status}
              </Badge>
              <MemberRoleAssignments
                organizationSlug={organizationSlug}
                member={member}
                canReadRoles={canReadRoles}
                canManageRoles={canManageRoles}
              />
              {canManageRoles && member.status === "active" && roles.data ? (
                <>
                  <label style={{ font: "var(--soa-font-caption)" }}>
                    Assign role{" "}
                    <select
                      aria-label={`Role for ${member.invited_email}`}
                      value={assignments[member.membership_id] ?? ""}
                      onChange={(event) =>
                        setAssignments((current) => ({
                          ...current,
                          [member.membership_id]: event.target.value,
                        }))
                      }
                    >
                      <option value="">Choose role</option>
                      {roles.data.map((role) => (
                        <option key={role.id} value={role.slug}>
                          {role.name}
                        </option>
                      ))}
                    </select>
                  </label>
                  <Button
                    size="sm"
                    variant="subtle"
                    isDisabled={!assignments[member.membership_id] || assignRole.isPending}
                    onPress={() => assignRole.mutate(member.membership_id)}
                  >
                    Assign
                  </Button>
                </>
              ) : null}
              {canManageMembers && member.status === "active" ? (
                <DialogTrigger>
                  <Button size="sm" variant="subtle">
                    Suspend
                  </Button>
                  <MemberStatusDialog
                    member={member}
                    target="suspended"
                    onConfirm={() => statusChange.mutate({ member, status: "suspended" })}
                  />
                </DialogTrigger>
              ) : null}
              {canManageMembers && member.status === "suspended" ? (
                <DialogTrigger>
                  <Button size="sm" variant="subtle">
                    Reactivate
                  </Button>
                  <MemberStatusDialog
                    member={member}
                    target="active"
                    onConfirm={() => statusChange.mutate({ member, status: "active" })}
                  />
                </DialogTrigger>
              ) : null}
              {canManageMembers && member.status !== "removed" ? (
                <DialogTrigger>
                  <Button size="sm" variant="destructive">
                    Remove
                  </Button>
                  <MemberStatusDialog
                    member={member}
                    target="removed"
                    onConfirm={() => statusChange.mutate({ member, status: "removed" })}
                  />
                </DialogTrigger>
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}
      {statusChange.isError || assignRole.isError ? (
        <Banner tone="critical" title="Member update failed">
          {failure(statusChange.error ?? assignRole.error, "The membership was not changed.")}
        </Banner>
      ) : null}
      {assignRole.isSuccess ? (
        <Banner tone="success" title="Role assigned">
          The role grant is active, audited, and shown on the member record.
        </Banner>
      ) : null}

      {roles.data ? (
        <div>
          <h3>Available roles</h3>
          <ul>
            {roles.data.map((role) => (
              <li key={role.id}>
                <strong>{role.name}</strong> <code>{role.slug}</code>{" "}
                <span style={{ color: "var(--soa-text-muted)" }}>
                  {role.permissions.length} permissions{role.is_system ? " · system role" : ""}
                </span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {roles.status === "error" ? (
        <Banner tone="critical" title="Couldn’t load roles">
          {failure(roles.error, "The role catalog is unavailable.")}
        </Banner>
      ) : null}
      {canManageRoles ? (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            createRole.mutate();
          }}
          style={{ display: "grid", gap: "var(--soa-space-3)", maxWidth: "44rem" }}
        >
          <h3 style={{ marginBottom: 0 }}>Create custom role</h3>
          <div style={{ display: "flex", gap: "var(--soa-space-3)", flexWrap: "wrap" }}>
            <TextField label="Role name" value={roleName} onChange={setRoleName} isRequired />
            <TextField label="Role slug" value={roleSlug} onChange={setRoleSlug} isRequired />
          </div>
          <TextField
            label="Permissions (comma separated)"
            description="Unknown or internal permissions are rejected by the server."
            value={rolePermissions}
            onChange={setRolePermissions}
            isRequired
          />
          <Button
            type="submit"
            isDisabled={!roleName.trim() || !roleSlug.trim() || !rolePermissions.trim()}
          >
            Create role
          </Button>
          {createRole.isError ? (
            <Banner tone="critical" title="Role creation failed">
              {failure(createRole.error, "No role was created.")}
            </Banner>
          ) : null}
        </form>
      ) : null}
    </Section>
  );
}

function CancelExportDialog({
  job,
  onConfirm,
}: {
  job: DataExportJob;
  onConfirm: (reason: string) => void;
}) {
  const [reason, setReason] = useState("");
  return (
    <Dialog title="Cancel data export?" alert>
      {({ close }) => (
        <div style={{ display: "grid", gap: "var(--soa-space-4)" }}>
          <p style={{ margin: 0 }}>The partial export stops. The cancellation reason is audited.</p>
          <TextField label="Reason" value={reason} onChange={setReason} isRequired />
          <div style={{ display: "flex", justifyContent: "flex-end", gap: "var(--soa-space-2)" }}>
            <Button variant="subtle" onPress={close}>
              Keep export running
            </Button>
            <Button
              variant="destructive"
              isDisabled={reason.trim().length < 3}
              onPress={() => {
                onConfirm(reason.trim());
                close();
              }}
            >
              Cancel export {job.id.slice(0, 8)}
            </Button>
          </div>
        </div>
      )}
    </Dialog>
  );
}

function DataExports({ organizationSlug }: { organizationSlug: string }) {
  const session = useShellSession();
  const queryClient = useQueryClient();
  const allowed = session.permissions.has("data.export");
  const exportsQuery = useQuery({
    queryKey: ["organization-data-exports", organizationSlug],
    queryFn: () => fetchOrganizationDataExports(organizationSlug),
    enabled: allowed,
    refetchInterval: (query) =>
      query.state.data?.items.some((job) => job.state === "pending" || job.state === "running")
        ? 5_000
        : false,
  });
  const create = useMutation({
    mutationFn: () => createOrganizationDataExport(organizationSlug),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["organization-data-exports", organizationSlug] }),
  });
  const cancel = useMutation({
    mutationFn: ({ id, reason }: { id: string; reason: string }) =>
      cancelOrganizationDataExport(organizationSlug, id, reason),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["organization-data-exports", organizationSlug] }),
  });

  return (
    <Section title="Organization data exports">
      {!allowed ? (
        <Banner tone="info" title="Additional permission required">
          Requesting or downloading a complete organization export requires data.export.
        </Banner>
      ) : (
        <>
          <p style={{ margin: 0 }}>
            Build an audited, expiring bundle of organization data. Generated links are short-lived
            and must not be shared publicly.
          </p>
          <Button onPress={() => create.mutate()} isDisabled={create.isPending}>
            Request organization export
          </Button>
          {create.isError || cancel.isError ? (
            <Banner tone="critical" title="Export action failed">
              {failure(create.error ?? cancel.error, "The export was not changed.")}
            </Banner>
          ) : null}
          {exportsQuery.status === "pending" ? <Skeleton height="6rem" /> : null}
          {exportsQuery.status === "error" ? (
            <Banner tone="critical" title="Couldn’t load organization exports">
              {failure(exportsQuery.error, "The export history is unavailable.")}
            </Banner>
          ) : null}
          {exportsQuery.data?.items.length === 0 ? (
            <p>No organization exports have been requested.</p>
          ) : null}
          {exportsQuery.data ? (
            <ul
              style={{
                listStyle: "none",
                margin: 0,
                padding: 0,
                display: "grid",
                gap: "var(--soa-space-3)",
              }}
            >
              {exportsQuery.data.items.map((job) => (
                <li
                  key={job.id}
                  style={{
                    borderBottom: "1px solid var(--soa-border)",
                    paddingBottom: "var(--soa-space-3)",
                  }}
                >
                  <div
                    style={{
                      display: "flex",
                      gap: "var(--soa-space-3)",
                      alignItems: "center",
                      flexWrap: "wrap",
                    }}
                  >
                    <code>{job.id.slice(0, 8)}</code>
                    <Badge
                      tone={
                        job.state === "succeeded"
                          ? "success"
                          : job.state === "failed"
                            ? "critical"
                            : "info"
                      }
                    >
                      {job.state}
                    </Badge>
                    <span>
                      {Math.round(job.progress * 100)}% · {job.processed_documents}/
                      {job.total_documents} documents
                    </span>
                    {job.manifest_download_url ? (
                      <a href={job.manifest_download_url} target="_blank" rel="noreferrer">
                        Download manifest
                      </a>
                    ) : null}
                    {job.state === "pending" || job.state === "running" ? (
                      <DialogTrigger>
                        <Button size="sm" variant="destructive">
                          Cancel
                        </Button>
                        <CancelExportDialog
                          job={job}
                          onConfirm={(reason) => cancel.mutate({ id: job.id, reason })}
                        />
                      </DialogTrigger>
                    ) : null}
                  </div>
                  {job.parts.length > 0 ? (
                    <ul>
                      {job.parts.map((part, index) =>
                        part.download_url ? (
                          <li key={`${part.name ?? part.category ?? "part"}-${index}`}>
                            <a href={part.download_url} target="_blank" rel="noreferrer">
                              Download {part.name ?? part.category ?? `part ${index + 1}`}
                            </a>
                          </li>
                        ) : null,
                      )}
                    </ul>
                  ) : null}
                  {job.safe_error ? (
                    <p style={{ color: "var(--soa-text-muted)" }}>{job.safe_error}</p>
                  ) : null}
                </li>
              ))}
            </ul>
          ) : null}
        </>
      )}
    </Section>
  );
}

function ServiceCredentials({ organizationSlug }: { organizationSlug: string }) {
  const session = useShellSession();
  const queryClient = useQueryClient();
  const allowed = session.permissions.has("credentials.manage");
  const [name, setName] = useState("");
  const [expiryDays, setExpiryDays] = useState("90");
  const [streamIds, setStreamIds] = useState<string[]>([]);
  const [oneTimeSecret, setOneTimeSecret] = useState<ServiceCredentialSecretResponse | null>(null);

  const credentials = useQuery({
    queryKey: ["service-credentials", organizationSlug],
    queryFn: () => fetchServiceCredentials(organizationSlug),
    enabled: allowed,
  });
  const streams = useQuery({
    queryKey: ["streams", organizationSlug],
    queryFn: () => fetchStreams(organizationSlug),
    enabled: allowed,
  });
  const refresh = () =>
    queryClient.invalidateQueries({ queryKey: ["service-credentials", organizationSlug] });
  const create = useMutation({
    mutationFn: () =>
      createServiceCredential(organizationSlug, {
        name: name.trim(),
        scopes: ["documents.upload"],
        allowed_stream_ids: streamIds,
        expires_in_days: Number(expiryDays),
      }),
    onSuccess: (result) => {
      setOneTimeSecret(result);
      setName("");
      setStreamIds([]);
      void refresh();
    },
  });
  const rotate = useMutation({
    mutationFn: (credential: ServiceCredentialEntry) =>
      rotateServiceCredential(organizationSlug, credential),
    onSuccess: (result) => {
      setOneTimeSecret(result);
      void refresh();
    },
  });
  const revoke = useMutation({
    mutationFn: (credential: ServiceCredentialEntry) =>
      revokeServiceCredential(organizationSlug, credential),
    onSuccess: () => {
      setOneTimeSecret(null);
      void refresh();
    },
  });
  const streamById = new Map(streams.data?.map((stream) => [stream.id, stream]) ?? []);
  const parsedExpiry = Number(expiryDays);

  return (
    <Section title="Service credentials">
      {!allowed ? (
        <Banner tone="info" title="Additional permission required">
          Creating and revoking public-ingestion keys requires credentials.manage.
        </Banner>
      ) : (
        <>
          <p style={{ margin: 0 }}>
            Keys are tenant-bound, limited to document upload, and accepted only for explicitly
            selected streams. A raw key appears once after creation or rotation.
          </p>
          {oneTimeSecret ? (
            <Banner tone="warning" title="Copy this API key now">
              <p>{oneTimeSecret.warning}</p>
              <code style={{ overflowWrap: "anywhere", userSelect: "all" }}>
                {oneTimeSecret.api_key}
              </code>
              <div style={{ marginTop: "var(--soa-space-3)" }}>
                <Button size="sm" variant="subtle" onPress={() => setOneTimeSecret(null)}>
                  I stored the key securely
                </Button>
              </div>
            </Banner>
          ) : null}
          {create.isError || rotate.isError || revoke.isError ? (
            <Banner tone="critical" title="Credential action failed">
              {failure(
                create.error ?? rotate.error ?? revoke.error,
                "The service credential was not changed.",
              )}
            </Banner>
          ) : null}
          <form
            onSubmit={(event) => {
              event.preventDefault();
              create.mutate();
            }}
            style={{ display: "grid", gap: "var(--soa-space-3)", maxWidth: "44rem" }}
          >
            <h3 style={{ marginBottom: 0 }}>Create ingestion key</h3>
            <div style={{ display: "flex", gap: "var(--soa-space-3)", flexWrap: "wrap" }}>
              <TextField label="Credential name" value={name} onChange={setName} isRequired />
              <TextField
                label="Expires in days"
                type="number"
                value={expiryDays}
                onChange={setExpiryDays}
                description="1–365 days"
                isRequired
              />
            </div>
            <fieldset
              style={{
                border: "1px solid var(--soa-border)",
                borderRadius: "var(--soa-radius-control)",
                display: "grid",
                gap: "var(--soa-space-2)",
                padding: "var(--soa-space-3)",
              }}
            >
              <legend>Allowed streams</legend>
              {streams.status === "pending" ? <Skeleton height="3rem" /> : null}
              {streams.status === "error" ? (
                <Banner tone="critical" title="Couldn’t load streams">
                  {failure(streams.error, "Stream choices are unavailable.")}
                </Banner>
              ) : null}
              {streams.data?.length === 0 ? (
                <span>Create a stream before creating a key.</span>
              ) : null}
              {streams.data?.map((stream) => (
                <Checkbox
                  key={stream.id}
                  isSelected={streamIds.includes(stream.id)}
                  isDisabled={stream.status === "archived"}
                  onChange={(selected) =>
                    setStreamIds((current) =>
                      selected ? [...current, stream.id] : current.filter((id) => id !== stream.id),
                    )
                  }
                >
                  {stream.name} <code>{stream.slug}</code>
                  {stream.status === "archived" ? " (archived)" : ""}
                </Checkbox>
              ))}
            </fieldset>
            <Button
              type="submit"
              isDisabled={
                create.isPending ||
                oneTimeSecret !== null ||
                !name.trim() ||
                streamIds.length === 0 ||
                !Number.isInteger(parsedExpiry) ||
                parsedExpiry < 1 ||
                parsedExpiry > 365
              }
            >
              Create service credential
            </Button>
          </form>

          {credentials.status === "pending" ? <Skeleton height="6rem" /> : null}
          {credentials.status === "error" ? (
            <Banner tone="critical" title="Couldn’t load service credentials">
              {failure(credentials.error, "Credential metadata is unavailable.")}
            </Banner>
          ) : null}
          {credentials.data?.items.length === 0 ? <p>No service credentials exist.</p> : null}
          {credentials.data?.items.length ? (
            <ul
              style={{
                listStyle: "none",
                margin: 0,
                padding: 0,
                display: "grid",
                gap: "var(--soa-space-3)",
              }}
            >
              {credentials.data.items.map((credential) => (
                <li
                  key={credential.id}
                  style={{
                    borderTop: "1px solid var(--soa-border)",
                    paddingTop: "var(--soa-space-3)",
                    display: "grid",
                    gap: "var(--soa-space-2)",
                  }}
                >
                  <div
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: "var(--soa-space-2)",
                      flexWrap: "wrap",
                    }}
                  >
                    <strong>{credential.name}</strong>
                    <Badge tone={credential.status === "active" ? "success" : "critical"}>
                      {credential.status}
                    </Badge>
                    <code>soa_{credential.key_prefix}_…</code>
                  </div>
                  <span>
                    Streams:{" "}
                    {credential.allowed_stream_ids.length
                      ? credential.allowed_stream_ids
                          .map(
                            (id) => streamById.get(id)?.name ?? `Unavailable (${id.slice(0, 8)})`,
                          )
                          .join(", ")
                      : "none (legacy key fails closed)"}
                  </span>
                  <span>
                    {credential.expires_at ? (
                      <>
                        Expires{" "}
                        <time dateTime={credential.expires_at}>{credential.expires_at}</time>
                      </>
                    ) : (
                      "No expiry (legacy key)"
                    )}
                    {credential.last_used_at
                      ? ` · last used ${credential.last_used_at}`
                      : " · never used"}
                  </span>
                  {credential.status === "active" ? (
                    <div style={{ display: "flex", gap: "var(--soa-space-2)" }}>
                      <DialogTrigger>
                        <Button size="sm" variant="subtle" isDisabled={oneTimeSecret !== null}>
                          Rotate {credential.name}
                        </Button>
                        <Dialog title={`Rotate ${credential.name}?`} alert>
                          {({ close }) => (
                            <div style={{ display: "grid", gap: "var(--soa-space-3)" }}>
                              <p style={{ margin: 0 }}>
                                The current key stops working immediately. The replacement expires
                                in 90 days and appears once.
                              </p>
                              <div
                                style={{
                                  display: "flex",
                                  justifyContent: "flex-end",
                                  gap: "var(--soa-space-2)",
                                }}
                              >
                                <Button variant="subtle" onPress={close}>
                                  Keep current key
                                </Button>
                                <Button
                                  variant="destructive"
                                  isDisabled={rotate.isPending || oneTimeSecret !== null}
                                  onPress={() => rotate.mutate(credential, { onSuccess: close })}
                                >
                                  Rotate key
                                </Button>
                              </div>
                            </div>
                          )}
                        </Dialog>
                      </DialogTrigger>
                      <DialogTrigger>
                        <Button size="sm" variant="destructive" isDisabled={oneTimeSecret !== null}>
                          Revoke {credential.name}
                        </Button>
                        <Dialog title={`Revoke ${credential.name}?`} alert>
                          {({ close }) => (
                            <div style={{ display: "grid", gap: "var(--soa-space-3)" }}>
                              <p style={{ margin: 0 }}>
                                This is immediate and cannot be undone. Any client using this key
                                will be denied.
                              </p>
                              <div
                                style={{
                                  display: "flex",
                                  justifyContent: "flex-end",
                                  gap: "var(--soa-space-2)",
                                }}
                              >
                                <Button variant="subtle" onPress={close}>
                                  Keep key active
                                </Button>
                                <Button
                                  variant="destructive"
                                  isDisabled={revoke.isPending}
                                  onPress={() => revoke.mutate(credential, { onSuccess: close })}
                                >
                                  Revoke key
                                </Button>
                              </div>
                            </div>
                          )}
                        </Dialog>
                      </DialogTrigger>
                    </div>
                  ) : null}
                </li>
              ))}
            </ul>
          ) : null}
        </>
      )}
    </Section>
  );
}

export function Settings() {
  const session = useShellSession();
  return (
    <AppShell
      title="Settings"
      breadcrumbs={[{ label: session.organization.name }, { label: "Settings" }]}
    >
      <div style={{ display: "grid", gap: "var(--soa-space-5)", maxWidth: "64rem" }}>
        <MembersAndRoles organizationSlug={session.organization.slug} />
        <ServiceCredentials organizationSlug={session.organization.slug} />
        <DataExports organizationSlug={session.organization.slug} />
      </div>
    </AppShell>
  );
}
