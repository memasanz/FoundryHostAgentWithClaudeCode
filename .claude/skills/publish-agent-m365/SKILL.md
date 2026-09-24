---
name: publish-agent-m365
description: >-
  Publish a private-network Microsoft Foundry agent to Microsoft 365 Copilot and Teams.
  Runs the REST publish flow (get agent identity -> create Azure Bot Service -> enable the
  source-IP-filtered Activity Protocol public endpoint via PATCH -> POST the M365 publish API).
  USE WHEN: publish agent to Teams, publish to M365 Copilot, Agent 365, enable_m365_public_endpoint,
  make a private Foundry agent visible in Teams. DO NOT USE FOR: creating/deploying the agent itself,
  or Foundry network/VNet provisioning.
---

# Publish a private-network Foundry agent to Microsoft 365 & Teams

Microsoft 365 **does not support private connectivity** to agents — it requires the agent's
**Activity Protocol** endpoint to be publicly routable. This skill publishes the private-network
agent by opening a scoped, **source-IP-filtered** public exception on *only* the Activity Protocol
route. Responses, MCP, A2A, agent-management APIs, and the Foundry→Fabric data path stay private.

> The `enable_m365_public_endpoint` PATCH is **Step 3 of 5** — a prerequisite, not the whole job.
> The agent will not appear in Agent 365 / Teams / M365 Copilot until Steps 1–4 all succeed.

Reference doc:
[Publish agents to M365 & Teams via REST](https://learn.microsoft.com/azure/foundry/agents/how-to/publish-copilot-virtual-network?view=foundry)

## Prerequisites (check before running)

1. **Roles**: `Foundry User` on the project + `Azure Bot Service Contributor` (or Contributor/Owner)
   on the resource group.
2. **Network location**: run from a client that can reach the project's **private endpoint**
   (VM in the VNet, VPN, or ExpressRoute). Management REST calls stay private.
3. **Persistent agent**: the target agent version must be **published and stable** (a fixed
   name/version), not an ephemeral test version.
4. **Provider registered**: `az provider register --namespace Microsoft.BotService`
5. **Signed in**: `az login` to the subscription that holds the Foundry resource.
6. **Python deps**: `pip install azure-identity requests`

## How to run

The workflow is automated by [`scripts/publish_agent_m365.py`](scripts/publish_agent_m365.py), which
uses [`scripts/bot_service.bicep`](scripts/bot_service.bicep) for the Bot Service resource.

> **Prefer a notebook?** [`publish_agent_m365.ipynb`](publish_agent_m365.ipynb) walks the same
> 5-step flow one cell at a time with **every HTTP request written out explicitly** (URL, headers,
> JSON body, raw response) — no helper-function abstraction. It defaults to `DRY_RUN = True` so the
> mutating cells print the exact request without sending it.

### 1. Inputs (the script asks you — no environment variables)

Just run the script; it **prompts interactively** for every value, shows a default in `[brackets]`
(press Enter to accept), and lists the project's agents so you can pick one by number or name.
It does **not** read environment variables.

| Prompt | Meaning | Default offered |
| --- | --- | --- |
| Project endpoint | `https://<res>.services.ai.azure.com/api/projects/<proj>` | *(required — you provide)* |
| Resource group | RG containing the Foundry resource | *(required — you provide)* |
| Agent name | Agent to publish (picker lists existing agents) | *(you choose)* |
| Publish scope | `Shared` \| `Personal` \| `Tenant` | `Shared` |
| Display name | Name shown in Teams/M365 | *(the agent name)* |
| App version | Semantic version; increment to republish | `1.0.0` |
| Short/Full description, Developer name, URLs | Store metadata | *(required — you provide each)* |
| Bot Service name | New bot to create (only if not reusing one) | *(you provide)* |

Every prompt can also be supplied up front as a `--flag` (see `--help`), which skips that prompt.
For automation/CI, pass `--no-prompt` to use flags + defaults without any interaction.

### 2. Check for an existing Bot Service (recommended first)

Before creating a new bot, see whether one is already associated with the agent. This reports the
agent's current M365 endpoint config (protocols, auth schemes, `enable_m365_public_endpoint`), the
**Foundry account's `publicNetworkAccess`** (so you know whether the PATCH is even required), and
scans the **whole subscription** for a Bot Service whose `endpoint` targets the agent or whose
`msaAppId` matches the agent identity:

```bash
python .claude/skills/publish-agent-m365/scripts/publish_agent_m365.py --check-bot
```

The scan is **subscription-wide by default** (so a bot in a different RG than the Foundry resource
is still found). To limit it to a single resource group, add `--scan-resource-group`:

```bash
python .claude/skills/publish-agent-m365/scripts/publish_agent_m365.py --check-bot --scan-resource-group
```

If a bot is found, reuse it instead of creating a duplicate:

```bash
python .claude/skills/publish-agent-m365/scripts/publish_agent_m365.py --skip-bicep --bot-arm-id <armId>
```

### 3. Test the full flow (dry run)

Print every request without mutating anything — use this to validate inputs and identity resolution:

```bash
python .claude/skills/publish-agent-m365/scripts/publish_agent_m365.py --dry-run
```

### 4. Publish

```bash
python .claude/skills/publish-agent-m365/scripts/publish_agent_m365.py
```
The script runs, in order:

1. **Get identity** — `GET {endpoint}/agents/{name}` → `instance_identity.client_id`; tenant via `az account show`.
2. **Create Bot Service** — first checks whether a bot is **already associated** with the agent
   (matching `msaAppId`/endpoint); if found it **reuses** it, otherwise it deploys `bot_service.bicep`
   (PNA disabled, Teams channel) and captures the ARM ID. Force a new one with `--force-bicep`.
3. **PATCH agent** *(only when required)* — first checks the Foundry account's `publicNetworkAccess`.
   If access is **Disabled or restricted**, it sets `activity.enable_m365_public_endpoint: true`
   (re-sending `responses`, `Entra`, and the scope-matched Bot Service scheme, since the PATCH
   **replaces** `protocol_configuration` and `authorization_schemes`). If the project allows
   **public** access, this step is **auto-skipped** (not needed — publish adds the protocol/scheme).
   Override with `--force-patch`, or skip the account lookup with `--no-network-check`.
4. **Publish** — `POST {endpoint}/agents/{name}/microsoft365/publish`, returns the `titleId`.

Use `--skip-bicep --bot-arm-id <id>` to reuse a specific Bot Service, or `--only patch,publish`
to re-run a subset.

> **Public (no network constraints) projects:** the flow still works. Step 3 auto-skips because the
> `enable_m365_public_endpoint` exception is only needed when public access is disabled. You can force
> it back on with `--force-patch`.

### 5. Scope ↔ auth scheme (must match)

| Publish scope | Auth scheme the PATCH sets | Visibility |
| --- | --- | --- |
| `Shared` / `Personal` | `BotServiceRbac` | You only; share by link, no admin approval |
| `Tenant` | `BotServiceTenant` | Whole tenant, after M365 admin approval |

The script picks the scheme automatically from the publish scope; keep them aligned or publishing
overwrites a mismatched scheme.

### 6. Verify

Open the M365/Teams agent store (`Shared` → **Your agents**; `Tenant` → **Built by your org** after
approval), send a message, and confirm a reply.

## How the source-IP filtering works

The IP filtering is **service-managed by the Foundry platform** — the skill, the script, and your
VNet do **not** configure or maintain any IP allowlist. You only flip one boolean
(`activity.enable_m365_public_endpoint: true` in Step 3); Foundry does the rest:

- Foundry applies a network exception **scoped to only the Activity Protocol route**. Per the docs:
  *"Service-managed source IP filtering allows requests delivered through Azure Bot Service or
  Microsoft 365 infrastructure and blocks requests from other public networks."*
- The allowed source ranges (Azure Bot Service / M365 channel infrastructure) are **maintained by
  Microsoft** on the platform side — there is no IP list in `bot_service.bicep`, the Python script,
  or an NSG you own.
- Filtering is **reachability, not identity** (defense-in-depth). Every request must *still* pass
  Bot Service token validation **plus** `Entra` / `BotServiceRbac` / `BotServiceTenant` auth;
  absent/spoofed source IPs fail closed.
- The exception is **inbound-only and route-only**: the Foundry account's `publicNetworkAccess`,
  the Responses/MCP/A2A/management APIs, and the Foundry→Fabric Private Link path are untouched.
- The **Azure Bot Service** (created in Step 2 with `publicNetworkAccess: 'Disabled'`) is the
  trusted proxy that relays Teams/Copilot messages to the private agent endpoint.

Reference: [Allow Microsoft 365 traffic to a private-network agent](https://learn.microsoft.com/azure/foundry/agents/how-to/configure-agent#allow-microsoft-365-traffic-to-a-private-network-agent)

## Key guardrails

- **PATCH replaces config** — always include every protocol (`responses`) and scheme (`Entra`) to retain.
- **No secrets in metadata** — publish-metadata fields are user-visible.
- **`appVersion`** — digits/periods only, cannot start with `0`; increment on each republish.
- **Data stays private** — only inbound Teams/M365 message delivery is public; Foundry→Fabric queries
  still traverse Private Link. Outbound/egress rules are unchanged.

## Limitations

- No file uploads / image generation in M365 (works in Teams).
- No streaming responses or citations for published agents.
- Channel traffic does not use Private Link (source-IP-filtered public route only).

## Testing

Hermetic unit tests live next to the script at
[`scripts/test_publish_agent_m365.py`](scripts/test_publish_agent_m365.py). They make **no**
real Azure, network, or `az` calls — every boundary (`run_az`, `request`, `requests.get`,
`input`) is monkeypatched — so they run offline in ~2s. They cover the scope→auth-scheme
mapping, the PATCH/publish request bodies, `enable_m365_public_endpoint` network-posture logic,
the interactive prompts (including required-field enforcement), and the agent picker.

```bash
# pytest
pytest .claude/skills/publish-agent-m365/scripts/test_publish_agent_m365.py
# or stdlib unittest (no pytest needed)
python -m unittest discover -s .claude/skills/publish-agent-m365/scripts -p "test_*.py"
```

## Sharing this skill

This skill is repo-local under `.claude/skills/`, so it travels with the repo for any contributor
using Claude Code. To use it across all your repos, copy the `publish-agent-m365/` folder into
`~/.claude/skills/` (personal scope). For the Claude desktop app, upload this `SKILL.md` via
**Settings → Skills → Add**. (It also works under `.github/skills/` for GitHub Copilot CLI.)
