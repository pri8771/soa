# `@soa/design-system`

The active React design-system workspace used by `apps/web`. It provides the
shared visual tokens, accessible primitives, and operations-specific
components that keep the application consistent without hiding product
behavior behind a generic dashboard kit.

## Contents

- `tokens.ts` / `tokens.css`: semantic color, typography, spacing, radius,
  shadow, and motion values for light and dark themes.
- `primitives.css`: shared focus, layout, form, table, and status styling.
- `components/`: React Aria-based buttons, fields, tabs, overlays, selection,
  toggles, toasts, statuses, data tables, banners, skeletons, and operational
  composites.
- `contrast.ts`: token contrast checks.
- `test/axe.ts`: reusable vitest-axe assertions.

## Verification

```bash
pnpm --filter @soa/design-system run typecheck
pnpm --filter @soa/design-system run test
```

There is no Storybook/workbench deployment in the repository today. Visual,
responsive, browser accessibility, and manual assistive-technology evidence
remain separate gates described in the UI blueprint and accessibility
checklist.
