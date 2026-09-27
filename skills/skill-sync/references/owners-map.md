# Owners map — which file owns which topic

Fill this in for your project. skill-sync reads it in step 3 to route every fact to exactly
ONE file. A topic without an owner is where duplicates start.

| Topic / subsystem | Owner skill | Owner file |
|---|---|---|
| (example) Authentication flow | `myapp-backend` | `references/auth.md` |
| (example) Deploy & infrastructure | `myapp-ops` | `references/deploy.md` |
| (example) Billing webhooks | `myapp-backend` | `references/billing.md` |

Rules:
- One row per topic. If two skills seem to own it, pick one and have the other point to it.
- Create new skills with `skill-new <name>` so they start with the 3-layer structure.
