# NextGen ERP — Experience & Performance Principles

The Experience Upgrade is designed around the following non-negotiable product principles:

## Performance budget
- Immediate actions are synchronous only when they are small; database/reporting work that can be deferred must not block the UI thread.
- Continuous decorative animation is disabled by default. One-shot transitions are also disabled by default so page changes and checkout remain immediate on low-spec computers.
- Avoid per-widget graphics effects, animated shadows, blur, and large image assets in the primary workflows.
- Use Qt-native widgets and painting; no embedded browser, web runtime, or large UI framework.
- Keep dashboard refreshes on demand or at a modest cadence, and pause background refresh/motion while the app is inactive.
- Load optional modules and heavy reports only when opened; preserve the current workspace and avoid rebuilding it during theme changes.
- Keep SQLite transactions short, indexes aligned to common searches, and paginate/limit long lists rather than loading entire histories into UI tables.
- Protect offline operation: no required network call during launch, login, POS, inventory, repairs, lending, or finance workflows.

## UX direction
1. Role-specific command-centre dashboard with a small set of actionable insights.
2. Task-first guided flows for sale, stock receipt, repair intake, lending, repayment, returns, and expense entry.
3. Universal search / command palette with keyboard access and permission-aware actions.
4. Consistent typography, spacing, status labels, warm light theme and restrained dark theme.
5. Empty states, recoverable errors, confirmation and audit context that explain the next step.
6. Maintainability: introduce changes in bounded components; preserve business rules, schema, roles, tenant/workspace isolation and data files.

## Quality gates
- Startup, login and workspace navigation should be tested on a low-resource Windows machine.
- No new background loop without a clear purpose and pause/stop lifecycle.
- Measure rather than guess: record cold launch, dashboard open, search, POS product lookup, and large inventory navigation timings before and after major changes.
- Any redesign must retain the functionality and data integrity of existing screens.
