# KunJin Phase 3 Learning, Reports, and Scheduling Plan

**Goal:** Add a local investment-thesis journal, evidence-structured weekly reports, and an installable weekday post-close synchronization job.

**Architecture:** SQLite schema version 3 stores user theses. Report commands compose existing portfolio, fund, and sector calculations without changing evidence levels. A standard-library plist generator creates but does not automatically load a macOS LaunchAgent until authorization is working.

### Tasks

- [ ] Add `investment_theses` schema and repository operations.
- [ ] Add `thesis add/list/review` CLI commands with explicit invalidation conditions.
- [ ] Add `sync daily` to refresh portfolio, held-fund NAV, and sectors with per-source errors.
- [ ] Add `report weekly` with facts, calculations, missing evidence, and learning prompts.
- [ ] Add and test a weekday 18:30 LaunchAgent installer.
- [ ] Update the isolated Skill, README, and full regression verification.
