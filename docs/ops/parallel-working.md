# Working in parallel — two consoles, one repo

> Moved from `CLAUDE.md` on 2026-10-20. `CLAUDE.md` keeps a short summary.

When two consoles work the same checkout at once (e.g. one on stats, one on
manifesto/templates), they SHARE one working tree, one HEAD, and one .git. File
edits to DIFFERENT files don't collide. The hazards are shared-state operations.

**1. Both consoles stay on the same branch (normally `beta`). No branch switching while the other is active.**
A `git checkout`/`switch`/`reset` in one console changes the working tree under the other mid-edit. If a console needs a different branch, coordinate first (tell the other, agree a pause).

**2. Edit different files.** Divide by area. If both need the same file, serialise it — one finishes and commits before the other starts.

**3. Master-promotion is SERIALISED — only ONE console pushes/merges to master at a time.**
Committing to beta in parallel is fine. Two consoles doing beta→master merge+push simultaneously collide. Before promoting to master, confirm the other console is NOT also about to. Live production fixes take master priority. One master-promotion in flight at a time, full stop.

**4. No uncommitted changes left floating while the other console is active.**
An uncommitted change is invisible to the other console and can be clobbered by a checkout, swept into the wrong commit, or forgotten. Commit it (WIP commit is fine) or `git stash` it.

**5. Explicit paths only — NEVER `git add -A`.** Doubly critical in parallel: it sweeps in the other console's uncommitted changes.

**6. No `reset`/`rebase`/branch-delete/force-push while the other console is active.** Coordinate first. Never unilaterally.

**7. When the situation changes, re-check stale instructions before running them.**
If something material changed since an instruction was given (e.g. a production bug was found), pause and re-confirm rather than executing the stale plan.

## `git worktree` for routine parallel work

If two-console work is routine, give each console its own `git worktree`
(separate directory + branch, shared history). That removes the shared-HEAD /
working-tree hazards (rules 1, 2, 4). Rule 3 — serialised master-promotion —
still applies because history is shared.
