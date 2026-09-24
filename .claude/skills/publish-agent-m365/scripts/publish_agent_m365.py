"""Publish a private-network Microsoft Foundry agent to Microsoft 365 Copilot and Teams.

Automates the REST publish flow documented at:
https://learn.microsoft.com/azure/foundry/agents/how-to/publish-copilot-virtual-network?view=foundry

Steps:
  1. Resolve the agent identity (instance_identity.client_id) and tenant ID.
  2. Create the Azure Bot Service resource (PNA disabled + Teams channel) via bot_service.bicep.
  3. PATCH the agent to enable the source-IP-filtered public Activity Protocol endpoint.
  4. POST the Microsoft 365 publish API.

Run from a client that can reach the project's private endpoint (VM in VNet / VPN / ExpressRoute).

Requires: azure-identity, requests, and the Azure CLI signed in (az login).
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import requests
from azure.identity import AzureCliCredential

API_VERSION = "v1"
ACTIVITY_API_VERSION = "2025-05-15-preview"
BICEP_FILE = Path(__file__).with_name("bot_service.bicep")

# publishScope -> Bot Service authorization scheme
SCOPE_TO_SCHEME = {
    "Shared": "BotServiceRbac",
    "Personal": "BotServiceRbac",
    "Tenant": "BotServiceTenant",
}

# Prompt defaults (offered interactively; not read from the environment).
# NOTE: project endpoint, resource group, and all store-metadata fields
# (descriptions, developer name, URLs) are intentionally *not* defaulted -
# they are environment/agent-specific and always prompted for (or passed via --flag).
DEFAULT_PUBLISH_SCOPE = "Shared"
DEFAULT_APP_VERSION = "1.0.0"


def parse_args():
    p = argparse.ArgumentParser(
        description="Publish a Foundry agent to Microsoft 365 & Teams. "
                    "Prompts interactively for anything not passed as a flag.")
    # All inputs default to None so we can tell what the caller supplied and
    # prompt for the rest. Nothing is read from environment variables.
    p.add_argument("--endpoint",
                   help="Project endpoint, e.g. https://<res>.services.ai.azure.com/api/projects/<proj>")
    p.add_argument("--agent-name", help="Agent to publish (prompts with a picker if omitted).")
    p.add_argument("--resource-group", help="Resource group that contains the Foundry resource.")
    p.add_argument("--bot-name", help="Name of the Azure Bot Service resource to create.")
    p.add_argument("--agent-display-name",
                   help="Display name in Teams/M365 (defaults to the agent name).")
    p.add_argument("--publish-scope", choices=["Shared", "Personal", "Tenant"])
    p.add_argument("--app-version")
    p.add_argument("--short-description")
    p.add_argument("--full-description")
    p.add_argument("--developer-name")
    p.add_argument("--developer-website-url")
    p.add_argument("--privacy-url")
    p.add_argument("--terms-of-use-url")
    p.add_argument("--bot-arm-id", help="Reuse an existing Bot Service ARM ID (implies --skip-bicep).")
    p.add_argument("--skip-bicep", action="store_true", help="Do not deploy the Bot Service resource.")
    p.add_argument("--force-bicep", action="store_true",
                   help="Deploy the Bot Service even if one is already associated with the agent.")
    p.add_argument("--force-patch", action="store_true",
                   help="Always run the enable_m365_public_endpoint PATCH, even if the account allows public access.")
    p.add_argument("--no-network-check", action="store_true",
                   help="Skip the Foundry account public-network-access check before the PATCH.")
    p.add_argument("--check-bot", action="store_true",
                   help="Only report current agent M365 config + any Bot Service already associated, then exit.")
    p.add_argument("--only", default="identity,bicep,patch,publish",
                   help="Comma list of steps to run: identity,bicep,patch,publish")
    p.add_argument("--no-prompt", action="store_true",
                   help="Never prompt; use flags and built-in defaults (for automation/CI).")
    p.add_argument("--dry-run", action="store_true", help="Print requests without mutating anything.")
    return p.parse_args()


def prompt(a, label, current, default=None, required=False, choices=None):
    """Return an already-supplied flag value, else interactively ask for it.

    With --no-prompt (or when stdin has no more input) falls back to the default.
    """
    if current is not None:
        return current
    if a.no_prompt:
        if required and default is None:
            sys.exit(f"Missing required value: {label} (pass the matching --flag or use --no-prompt).")
        return default
    hint = f" {choices}" if choices else ""
    suffix = f" [{default}]" if default is not None else ""
    while True:
        try:
            value = input(f"{label}{hint}{suffix}: ").strip()
        except EOFError:
            if required and default is None:
                sys.exit(f"Missing required value: {label} (pass the matching --flag or use --no-prompt).")
            return default
        if not value:
            value = default
        if not value and required:
            print("  A value is required.")
            continue
        if choices and value not in choices:
            print(f"  Choose one of {choices}.")
            continue
        return value


def choose_agent(a, token):
    """Prompt for the agent, listing what exists in the project when possible."""
    if a.agent_name is not None:
        return a.agent_name
    if a.no_prompt:
        return prompt(a, "Agent name", None, required=True)
    names = []
    if not a.dry_run:
        url = f"{a.endpoint}/agents?api-version={API_VERSION}"
        try:
            resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=60)
            if resp.ok:
                names = [x.get("name") for x in resp.json().get("data", []) if x.get("name")]
        except requests.RequestException:
            pass
    if names:
        print("Agents in this project:")
        for i, name in enumerate(names, 1):
            print(f"  {i}. {name}")
        try:
            raw = input("Select an agent (number or name): ").strip()
        except EOFError:
            raw = ""
        if raw.isdigit() and 1 <= int(raw) <= len(names):
            return names[int(raw) - 1]
        if raw:
            return raw
    return prompt(a, "Agent name", None, required=True)


def resolve_config(a, token):
    """Fill in every input via flags first, then interactive prompts."""
    a.endpoint = prompt(a, "Project endpoint (https://<res>.services.ai.azure.com/api/projects/<proj>)",
                        a.endpoint, required=True)
    a.endpoint = a.endpoint.rstrip("/")
    a.resource_group = prompt(a, "Resource group (contains the Foundry resource)",
                              a.resource_group, required=True)
    a.agent_name = choose_agent(a, token)
    if a.check_bot:
        return
    a.publish_scope = prompt(a, "Publish scope", a.publish_scope, DEFAULT_PUBLISH_SCOPE,
                             choices=["Shared", "Personal", "Tenant"])
    a.agent_display_name = prompt(a, "Display name", a.agent_display_name, a.agent_name)
    a.app_version = prompt(a, "App version", a.app_version, DEFAULT_APP_VERSION, required=True)
    a.short_description = prompt(a, "Short description", a.short_description, required=True)
    a.full_description = prompt(a, "Full description", a.full_description, required=True)
    a.developer_name = prompt(a, "Developer name", a.developer_name, required=True)
    a.developer_website_url = prompt(a, "Developer website URL", a.developer_website_url,
                                     required=True)
    a.privacy_url = prompt(a, "Privacy URL", a.privacy_url, required=True)
    a.terms_of_use_url = prompt(a, "Terms of use URL", a.terms_of_use_url, required=True)
    if not a.bot_arm_id and not a.skip_bicep:
        a.bot_name = prompt(a, "Bot Service name to create", a.bot_name, required=True)


def run_az(args, dry_run=False, check=True):
    cmd = ["az", *args]
    if dry_run:
        print(f"[dry-run] {' '.join(cmd)}")
        return ""
    result = subprocess.run(cmd, capture_output=True, text=True, shell=(os.name == "nt"))
    if result.returncode != 0:
        if not check:
            return None
        sys.exit(f"az command failed: {' '.join(cmd)}\n{result.stderr.strip()}")
    return result.stdout.strip()


def account_name(a):
    """Derive the Foundry (AI Services) account name from the project endpoint host."""
    host = urlparse(a.endpoint).hostname or ""
    return host.split(".")[0]


def network_posture(a):
    """Best-effort management-plane check of the parent Foundry account.

    Returns (public_network_access, is_restricted, resolved). `is_restricted` is
    True when public access is Disabled or restricted by network ACLs — i.e. when
    the enable_m365_public_endpoint PATCH is required.
    """
    if a.dry_run:
        return (None, True, False)
    name = account_name(a)
    out = run_az([
        "cognitiveservices", "account", "show",
        "--name", name, "--resource-group", a.resource_group,
        "--query",
        "{pna:properties.publicNetworkAccess,ip:properties.networkAcls.ipRules,"
        "vnet:properties.networkAcls.virtualNetworkRules}",
        "-o", "json",
    ], check=False)
    if not out:
        return (None, True, False)
    try:
        d = json.loads(out)
    except json.JSONDecodeError:
        return (None, True, False)
    pna = d.get("pna")
    restricted = (pna == "Disabled") or bool(d.get("ip")) or bool(d.get("vnet"))
    return (pna, restricted, pna is not None)


def get_token(credential):
    return credential.get_token("https://ai.azure.com/.default").token


def request(method, url, token, dry_run, json_body=None, content_type="application/json"):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": content_type}
    if dry_run:
        print(f"[dry-run] {method} {url}")
        if json_body is not None:
            print(json.dumps(json_body, indent=2))
        return {}
    resp = requests.request(method, url, headers=headers, json=json_body, timeout=60)
    if not resp.ok:
        sys.exit(f"{method} {url} failed: {resp.status_code}\n{resp.text}")
    return resp.json() if resp.text else {}


def step_identity(a, token):
    url = f"{a.endpoint}/agents/{a.agent_name}?api-version={API_VERSION}"
    print(f"==> Getting agent identity: GET {url}")
    body = request("GET", url, token, a.dry_run)
    client_id = (body.get("instance_identity") or {}).get("client_id") if not a.dry_run else "<client-id>"
    tenant_id = run_az(["account", "show", "--query", "tenantId", "-o", "tsv"], a.dry_run) or "<tenant-id>"
    print(f"    client_id={client_id} tenant_id={tenant_id}")
    return client_id, tenant_id, body


def list_bots(a):
    """List Bot Service resources in the resource group as (armId, endpoint, msaAppId).

    Uses the ARM provider API via `az rest` because `az resource list` does not
    reliably index Microsoft.BotService resources.
    """
    sub = run_az(["account", "show", "--query", "id", "-o", "tsv"], check=False)
    if not sub:
        return []
    url = (f"https://management.azure.com/subscriptions/{sub}/resourceGroups/"
           f"{a.resource_group}/providers/Microsoft.BotService/botServices?api-version=2022-09-15")
    out = run_az(["rest", "--method", "get", "--url", url, "--query", "value", "-o", "json"], check=False)
    if not out:
        return []
    try:
        items = json.loads(out)
    except json.JSONDecodeError:
        return []
    bots = []
    for b in items:
        props = b.get("properties", {}) or {}
        bots.append((b.get("id"), props.get("endpoint") or "", props.get("msaAppId") or ""))
    return bots


def find_associated_bot(a, client_id):
    """Return (matching_bot, all_bots). A bot matches when its msaAppId equals the
    agent identity client_id, or its endpoint targets this agent."""
    bots = list_bots(a)
    needle = f"/agents/{a.agent_name}/"
    for bid, endpoint, msa in bots:
        if (client_id and msa == client_id) or (needle in endpoint):
            return (bid, endpoint, msa), bots
    return None, bots


def check_association(a, token):
    """Report the agent's current M365 config and any Bot Service already wired to it."""
    client_id, _, body = step_identity(a, token)

    endpoint_cfg = (body.get("agent_endpoint") or {}) if not a.dry_run else {}
    proto = endpoint_cfg.get("protocol_configuration", {}) or {}
    schemes = [s.get("type") for s in endpoint_cfg.get("authorization_schemes", []) or []]
    activity = proto.get("activity") or {}
    print("--- Agent endpoint configuration ---")
    print(f"    protocols:            {', '.join(proto.keys()) or '(none)'}")
    print(f"    authorization schemes: {', '.join(schemes) or '(none)'}")
    print(f"    enable_m365_public_endpoint: {activity.get('enable_m365_public_endpoint', False)}")
    bot_scheme_set = any(s and s.startswith('BotService') for s in schemes)
    print(f"    Looks M365-configured: {bot_scheme_set and bool(activity.get('enable_m365_public_endpoint'))}")

    pna, restricted, resolved = network_posture(a)
    print("--- Foundry account network posture ---")
    if resolved:
        print(f"    publicNetworkAccess: {pna} (restricted={restricted})")
        print(f"    enable_m365_public_endpoint PATCH required: {restricted}")
    else:
        print("    Could not determine (need reader on the Foundry account); PATCH assumed required.")

    if not a.resource_group:
        print("    (Set RESOURCE_GROUP to scan for an associated Bot Service.)")
        return
    print(f"--- Scanning Bot Services in {a.resource_group} for '{a.agent_name}' ---")
    if a.dry_run:
        return
    match, bots = find_associated_bot(a, client_id)
    if match:
        bid, endpoint, msa = match
        print(f"    FOUND associated bot")
        print(f"      armId:    {bid}")
        print(f"      endpoint: {endpoint}")
        print(f"      msaAppId: {msa}  (matches agent: {msa == client_id})")
        print(f"      Reuse it with:  --skip-bicep --bot-arm-id {bid}")
    else:
        print(f"    No Bot Service in this resource group is associated with this agent "
              f"({len(bots)} bot(s) scanned).")



def step_bicep(a, client_id, tenant_id):
    if a.bot_arm_id:
        print(f"==> Reusing Bot Service: {a.bot_arm_id}")
        return a.bot_arm_id
    if not a.resource_group or not a.bot_name:
        sys.exit("RESOURCE_GROUP and BOT_NAME are required to create the Bot Service.")
    if not a.force_bicep and not a.dry_run:
        match, _ = find_associated_bot(a, client_id)
        if match:
            bid, endpoint, msa = match
            print(f"==> Existing Bot Service already associated with '{a.agent_name}' - reusing it")
            print(f"    armId={bid}  (use --force-bicep to create a new one instead)")
            return bid
    activity_endpoint = (
        f"{a.endpoint}/agents/{a.agent_name}/endpoint/protocols/activityProtocol"
        f"?api-version={ACTIVITY_API_VERSION}"
    )
    display = a.agent_display_name or a.agent_name
    print(f"==> Deploying Bot Service '{a.bot_name}' (Teams channel, PNA disabled)")
    arm_id = run_az([
        "deployment", "group", "create",
        "--resource-group", a.resource_group,
        "--template-file", str(BICEP_FILE),
        "--parameters",
        f"botName={a.bot_name}",
        f"displayName={display}",
        f"msaAppId={client_id}",
        f"tenantId={tenant_id}",
        f"endpoint={activity_endpoint}",
        "--query", "properties.outputs.botServiceArmId.value", "-o", "tsv",
    ], a.dry_run) or "<bot-arm-id>"
    print(f"    botServiceArmId={arm_id}")
    return arm_id


def step_patch(a, token):
    scheme = SCOPE_TO_SCHEME[a.publish_scope]
    url = f"{a.endpoint}/agents/{a.agent_name}?api-version={API_VERSION}"
    body = {
        "agent_endpoint": {
            "protocol_configuration": {
                "responses": {},
                "activity": {"enable_m365_public_endpoint": True},
            },
            "authorization_schemes": [
                {"type": "Entra"},
                {"type": scheme},
            ],
        }
    }
    print(f"==> Enabling public Activity Protocol endpoint (scheme={scheme}): PATCH {url}")
    request("PATCH", url, token, a.dry_run, json_body=body,
            content_type="application/merge-patch+json")


def step_publish(a, token, bot_arm_id):
    if not bot_arm_id:
        sys.exit("A Bot Service ARM ID is required to publish (run the bicep step or pass --bot-arm-id).")
    url = f"{a.endpoint}/agents/{a.agent_name}/microsoft365/publish?api-version={API_VERSION}"
    body = {
        "agentDisplayName": a.agent_display_name or a.agent_name,
        "botServiceArmId": bot_arm_id,
        "publishScope": a.publish_scope,
        "publishAsAutopilot": False,
        "appVersion": a.app_version,
        "shortDescription": a.short_description,
        "fullDescription": a.full_description,
        "developerName": a.developer_name,
        "developerWebsiteUrl": a.developer_website_url,
        "privacyUrl": a.privacy_url,
        "termsOfUseUrl": a.terms_of_use_url,
    }
    print(f"==> Publishing to Microsoft 365 (scope={a.publish_scope}): POST {url}")
    result = request("POST", url, token, a.dry_run, json_body=body)
    title_id = result.get("titleId", "<titleId>")
    print(f"    Published. titleId={title_id}")


def main():
    a = parse_args()
    if a.bot_arm_id:
        a.skip_bicep = True
    steps = {s.strip() for s in a.only.split(",") if s.strip()}

    credential = AzureCliCredential()
    token = get_token(credential) if not a.dry_run else "<token>"

    resolve_config(a, token)

    if a.check_bot:
        check_association(a, token)
        return

    client_id, tenant_id = ("<client-id>", "<tenant-id>")
    if "identity" in steps:
        client_id, tenant_id, _ = step_identity(a, token)

    bot_arm_id = a.bot_arm_id
    if "bicep" in steps and not a.skip_bicep:
        bot_arm_id = step_bicep(a, client_id, tenant_id)
    elif a.bot_arm_id:
        bot_arm_id = a.bot_arm_id

    if "patch" in steps:
        run_patch = True
        if not a.force_patch and not a.no_network_check:
            pna, restricted, resolved = network_posture(a)
            if resolved and not restricted:
                print(f"==> Skipping PATCH: Foundry account '{account_name(a)}' publicNetworkAccess="
                      f"'{pna}' (unrestricted) - enable_m365_public_endpoint not required for public projects.")
                run_patch = False
            elif resolved:
                print(f"    Foundry account publicNetworkAccess='{pna}' (restricted) - PATCH required.")
            else:
                print("    Could not determine Foundry account network posture - running PATCH (safe default).")
        if run_patch:
            step_patch(a, token)

    if "publish" in steps:
        step_publish(a, token, bot_arm_id)

    print("\nDone. Verify in the M365/Teams agent store, then send the agent a message.")


if __name__ == "__main__":
    main()
