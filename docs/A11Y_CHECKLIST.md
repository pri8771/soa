# Manual Accessibility Checklist (DSN-009)

Automated checks (vitest-axe unit sweeps + Playwright axe on rendered pages,
both required in CI) catch structural violations. The checks below need a
human and MUST be walked through before any paid-pilot release
(UI_UX_BLUEPRINT §8/§15), and whenever one of the covered flows changes
materially. Record the run date, build, and findings in the PR.

## Critical flows to walk through

For each flow: keyboard only (no mouse), then with a screen reader
(NVDA/VoiceOver), at 200% zoom, and in both themes.

### 1. Sign-in and organization selection
- [ ] Every control reachable and operable by keyboard in a logical order
- [ ] Errors are announced and linked to the fields they concern
- [ ] Focus is visible on every interactive element

### 2. Documents queue
- [ ] Sort controls announce the resulting order (aria-sort verified audibly)
- [ ] Row selection state is announced; bulk bar count is read on change
- [ ] Filter changes announce result updates; empty state is discoverable

### 3. Upload
- [ ] Drag-and-drop has a keyboard alternative
- [ ] Per-file progress and failures are announced
- [ ] Partial failure is not summarized as success

### 4. Review Studio
- [ ] Full keyboard map (J/K, Tab, E, M, A, R, [, ], ?) works without a mouse
- [ ] Selecting a field announces its evidence region description
- [ ] Autosave state changes (saved/saving/conflict) are announced
- [ ] Approval summary readable and operable before the approve action

### 5. Approval and conflict recovery
- [ ] Conflict UI names both values and who made the other change
- [ ] Dialogs trap focus, close on Escape, and restore focus to the trigger

### 6. Stream configuration
- [ ] Inherited/overridden/not-configured labels are exposed to AT
- [ ] Publish impact summary is readable before confirmation

### 7. Integration failure recovery
- [ ] Failure banner uses role=alert exactly once (no announcement spam)
- [ ] Retry/replay controls state what will happen

## Global checks
- [ ] Reduced-motion preference removes non-essential animation
- [ ] No information is conveyed by color alone (spot-check status-heavy views)
- [ ] Touch targets in touch-oriented layouts are at least 44px
- [ ] Zoom to 200% loses no critical function on queue and review views
