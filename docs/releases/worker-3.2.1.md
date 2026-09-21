# Worker 3.2.1

Release candidate for the access-lifecycle fixes in [PR #11](https://github.com/IvanTopGaming/ITG_xray_panel/pull/11).

## Release scope

| Component | Version |
|---|---|
| Worker image | `ghcr.io/ivantopgaming/panel-worker:v3.2.1` |
| Xray runtime and validation binary | `ghcr.io/xtls/xray-core:26.3.27` |
| Xray protobuf source | `v26.3.27` |
| Database schema | 29, unchanged from worker 3.2.0 |

Only the worker version changes in `versions.json`. Master, bot-api, cron, subscriptions, frontends,
Caddy and egress keep their existing versions. The shared Python modules changed here execute these
operations on the worker; remote callers retain the same API and payloads.

The release and development build workflows select the Xray binary from the same version as the
protobuf source. The node example and local worker build default also pin this release instead of
`latest`. Updating an existing installation does not automatically change its `XRAY_IMAGE` setting.

## Changes

- Provisioning and renewal use gRPC for VLESS/VMess activation changes. An active renewal leaves
  the runtime user and connections intact.
- Expiry, quota enforcement, account block/unblock, entitlement revocation and traffic-cycle
  reactivation use the same incremental path. Counter-only changes do not restart the core.
- Paid renewal of an expired legacy key can enable access again. Explicit manual disables remain
  in force; historical expiry/quota disables are distinguished from manual ones.
- Retrying an account block does not convert its own disable into a manual disable, so a later
  unblock can restore access.
- When the selected entitlement expires, the limit job settles its traffic and restores the
  remaining source's quota and usage. Valid manual extensions and account blocks are preserved.
- Config validation, durable desired/applied revisions, idempotent receipts and recovery remain
  in place. Unsupported protocols, activation changes with custom routing, gRPC failures and older
  pending revisions retain the full-apply fallback.

## Disabling semantics

Removing a VLESS/VMess user prevents new authentication. Already authenticated connections are
allowed to finish naturally, including after expiry, quota exhaustion, account blocking or
revocation. A sustained stream can therefore keep transferring beyond the limit. This is the
selected behavior; this release does not promise immediate per-user connection termination.

Full-apply fallback and restarting the backend during deployment can still restart Xray. Routine
incremental user operations do not require a node-wide restart.

## Validation

The application change passed 2802 backend tests, with 46 environment-dependent skips, and 53
reliability checks using the pinned Xray binary. Coverage includes payment delivery to legacy keys,
idempotent retries, failed-runtime recovery, quota/source transitions and VLESS/VMess live users.
Four real VLESS TCP tests additionally verify that new connections are refused while existing own
and unrelated-user streams survive. Independent code review found no outstanding issues under the
selected disabling contract.

The release-preparation checks execute both workflow build scripts with a substituted Xray version
and verify the binary/protobuf build arguments and node/local-build pins. CI must be green on the
final PR head before merging.

The final worker container image is built and pushed by the release workflow after merge. A local
Docker image build was not performed because the local Docker daemon is unavailable.

## Publish

1. Squash-merge PR #11 into `main` after the final checks pass.
2. Wait for **Release (versions.json-driven)** to succeed. The version diff must select only
   `worker`; the expected platform is `linux/amd64`.
3. Verify `ghcr.io/ivantopgaming/panel-worker:v3.2.1` exists and record its registry digest before
   deployment. Do not deploy a candidate tag before that build has completed.

```bash
docker buildx imagetools inspect ghcr.io/ivantopgaming/panel-worker:v3.2.1
```

## Update existing nodes

Update one node first, verify it, then update the remaining nodes. The backend startup synchronizes
Xray, so expect a deployment-time interruption on each node being upgraded.

1. Export the node backup from the panel and retain its current `.env` and worker image reference
   outside the deployment directory. Keep the existing Xray image digest for rollback as well.
2. In the node's `.env`, set `WORKER_IMAGE=ghcr.io/ivantopgaming/panel-worker:v3.2.1`. Set
   `XRAY_IMAGE=ghcr.io/xtls/xray-core:26.3.27` to retain the tested core on later pulls. Check the
   running core version first: these rollout commands assume it is already 26.3.27.
3. From that node's existing deployment directory, update only its backend:

```bash
docker compose -f docker-compose.node.yml pull backend
docker compose -f docker-compose.node.yml up -d --no-deps --wait --wait-timeout 300 backend
docker compose -f docker-compose.node.yml ps backend xray
docker compose -f docker-compose.node.yml logs --since 5m backend
```

If the running Xray is a different version, align it to the tested version in a separate planned
core update before using this backend-only procedure. Avoid pulling the whole stack just to update
the worker, especially when existing third-party image references still use `latest`.

Verify the node is online in the master and reports worker 3.2.1. With a dedicated test account,
check grant/renewal, an expired legacy key, repeated block/unblock and quota/source transitions.
Check subscription delivery after the normal polling/cache interval. On normal VLESS/VMess user
changes, Xray's start time should stay unchanged; confirm no pending runtime-apply errors remain.

These fixes act on subsequent operations. They do not mass-enable old disabled keys or repair
ambiguous historical manual flags. Review any already affected account individually.

## Rollback

Restore the previous worker reference in `.env` and repeat the backend-only pull/up commands.
There is no schema migration in this release, so rolling back to the previously running worker
3.2.0 does not require restoring the database. Restoring an old database would discard newer
payments/access changes and is not part of this rollback.

Keep the current database, `shared_config`, `xray_logs` and Caddy volumes. Rollback restores the
previous restart/legacy-access behavior too; it does not undo access changes already committed.
