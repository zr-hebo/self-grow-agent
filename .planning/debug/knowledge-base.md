# GSD Debug Knowledge Base

Resolved debug sessions. Used by `gsd-debugger` to surface known-pattern hypotheses at the start of new investigations.

---

## revise-route-version-conflict — Fixed route-version conflict persisted because deployment was one commit behind
- **Date:** 2026-09-03
- **Error patterns:** `revise-and-implement`, `expected route version`, `current version is`, stale `route_version`, deployment version mismatch
- **Root cause(s):** Deployment ran `e3974aa`, whose old revise worker used persisted requirement version 3 instead of the active runtime version; persisted requirement version 3 and active runtime version 4 jointly triggered the optimistic-concurrency conflict
- **Fix:** Push and deploy commit `46efcf5`, restart the service, and submit a fresh operation; no additional source change was required
- **Files changed:** none
- **Why not caught:** No deployment-revision verification gate existed; focused tests validated local `46efcf5` but did not prove that the commit had reached and been loaded by the deployment
- **Recurrence guard:** This knowledge-base pattern requires comparing deployment `HEAD`, remote `refs/heads/main`, and the expected fix commit before modifying source for a post-fix recurrence
---

