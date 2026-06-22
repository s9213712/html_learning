# AI Agent Function Audit: Community And Governance

Date: 2026-06-22
Target: `https://127.0.0.1:54384`
Artifact: `/tmp/hackme_ai_agent_frontend_audit_20260622_54384/reports/ai_agent_governance_capability_probe_v2.json`

## Scope

This pass audited forum posting/reply, member create/update and sanction-like updates, bug bounty rewards, community reward/penalty, member governance APIs, and AI Agent permission boundaries.

## Result Table

| ID | Item | Result | Evidence |
| --- | --- | --- | --- |
| GOV-01 | AI Agent governance-adjacent tools | PASS | AI Agent exposes `write_community_create_thread`, `write_community_reply_thread`, `write_member_create_user`, `write_member_update_user`, and `write_bug_report_review`. |
| GOV-02 | Forum post and reply | PASS | AI Agent created thread `3` on board `1` and reply post `3`. |
| GOV-03 | Member create / sanction-like update | PASS | AI Agent created user `ai_gov_*` and updated the user to restricted/sanctioned fields through existing member API. |
| GOV-04 | Bug bounty reward | PASS | AI Agent reviewed a synthetic bug report and awarded 1 point through `write_bug_report_review`. |
| GOV-05 | Site community reward/penalty/governance APIs exist | PASS | Direct site APIs for thread reward, post penalty, and moderation proposal list returned 200. |
| GOV-06 | AI Agent community reward/governance tools | GAP | 8/8 proposed tools were rejected as unsupported: member reward/penalty, community reward/penalty, governance proposal/vote/execute, emergency governance action. |
| GOV-07 | Dialogue governance request | GAP | Natural-language governance request caused no write-tool request and responded in 0.528s without claiming success. |
| GOV-08 | Permission and overreach boundary | PASS | normal member got 403 for write-tool list and 403 for member governance write attempt. |
| GOV-09 | Response time / repetition | PASS | Governance boundary response 0.528s; no empty message or adjacent repeat. |

## Capability Boundary

- AI Agent can create forum threads/replies, create/update members, and review bug reports with rewards.
- AI Agent can apply sanction-like member fields only through the generic member update tool; it does not model a complete governance workflow.
- The site has direct community reward/penalty and moderation governance APIs, but AI Agent cannot invoke them.
- The current behavior is safe: unsupported governance actions are rejected and member users cannot execute write tools.

## Fix Direction

Add explicit governance tools instead of overloading generic member update:

1. `write_community_reward_thread` and `write_community_penalty_post`.
2. `write_member_reward` and `write_member_penalty` with reason, scope, expiry, and appeal metadata.
3. `write_governance_proposal_create`, `write_governance_vote`, and `write_governance_execute`.
4. `write_emergency_governance_action` only for predeclared emergency actions already supported by the site.

All governance writes should require root/manager policy checks, structured reason text, target preview, explicit confirmation, audit rows, and appeal/rollback references.

## Next Function

Recommended next scoped audit: cloud drive, sharing, task management, and automation jobs.
