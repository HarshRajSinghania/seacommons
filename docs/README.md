# SeaCommons documentation

This index distinguishes canonical design documents from operational runbooks,
historical evidence and forward-looking plans. Start here instead of choosing
the newest dated filename.

## Public documentation

- [Public documentation contract](PUBLIC_DOCUMENTATION.md) — ownership of `seacommons.org/docs`, public information architecture, routing and metric semantics.
- Public reader entrypoint: **https://seacommons.org/docs**.
- Generated API reference: **https://api.seacommons.org/docs**.

## Canonical design

- [Architecture](ARCHITECTURE.md) — deployable surfaces, process topologies,
  storage ownership and invariants.
- [Data flow](DATA_FLOW.md) — ingestion, persistence, projection, Public Live and
  drift flows.
- [Security model](SECURITY_MODEL.md) — trust boundaries, authentication,
  webhook authenticity and public/private policy.
- [Maritime-domain compartments](COMPARTMENTS.md) — the compartment taxonomy
  (SAR, sanctions/dark-fleet, grey-zone, piracy, …), sources per compartment,
  and each one's public/private posture.
- [OSINT cross-source fusion](OSINT_FUSION.md) — the correlation engine that
  turns independent signals into `correlated_alert` events + auto-opened cases,
  its rules, the case bridge, and the operator alert rail.
- [Realtime architecture](REALTIME_ARCHITECTURE.md) — delivery semantics,
  ordering, fallbacks and recovery.
- [Testing strategy](TESTING.md) — the testing pyramid and the map from the ten
  highest-risk flows to the tests that guard them.
- [Deployment](DEPLOYMENT.md) — deployment modes, required production
  configuration, rollout and rollback order.
- [Development](DEVELOPMENT.md) — local setup, the dev stack, test/lint commands
  and common contributor tasks.
- [Configuration](CONFIGURATION.md) — environment variables grouped by concern,
  production requirements and combination validation.
- [Operations overview](OPERATIONS_OVERVIEW.md) — the real data sources, their
  cost, the compute footprint, and what to check when something looks wrong.
- [AI-assisted engineering policy](AI_ENGINEERING_POLICY.md) — rules for changes
  developed with AI coding tools.
- [Contract catalogue](contracts/README.md) — versioned machine-readable
  payload contracts.
- [Architecture decisions](adr/README.md) — durable decisions and trade-offs.

These documents describe current intended behavior. If a dated audit conflicts
with them, the canonical documents and executable tests take precedence.

## Operational runbooks

- [Production runbook](PRODUCTION_RUNBOOK.md)
- [Pilot runbook](PILOT_RUNBOOK.md)
- [Federated Live runbook](FEDERATED_LIVE_RUNBOOK.md)
- [Cloudflare edge deployment](CLOUDFLARE_EDGE_DEPLOYMENT.md)
- [Oracle deployment](DEPLOY_CLOUDFLARE_ORACLE.md)
- [Live cutover and rollback](LIVE_FIRST_CUTOVER.md)

Runbooks contain procedures and environment-specific commands. Validate them
against the current deployment manifests before use; a runbook is not the
architecture source of truth.

## Historical audits and evidence

The following files are retained for traceability. They are point-in-time
observations and are superseded as architecture descriptions by the canonical
documents above:

- `DOMAIN_FTP_AUDIT_2026-07-15.md`
- `ORACLE_MIGRATION_AUDIT_2026-07-15.md`
- `PRODUCTION_AUDIT_2026-07-15.md`
- `RESEARCH_AUDIT_2026-07-15.md`
- `LIVE_AUDIT_2026-08-24.md`
- `ENGINEERING_AUDIT.md`

`ENGINEERING_AUDIT.md` holds the baseline audit. [`roadmap.md`](roadmap.md) is
the production-hardening plan built on that audit, with a progress log tracking
the state of each item. The canonical documents above reflect the resulting
architecture.

## Plans and research proposals

Files such as `SIMULATION_DEMO_PLAN.md`, `UNREAL_PIXEL_STREAMING_PLAN.md`,
`VESSEL_INTEGRATION_PLAN.md`, `UI_MOBILE_REDESIGN.md` and `PILOT_PHASE0.md`
describe proposed or staged work. They are not claims that functionality is
deployed.
