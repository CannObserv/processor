---
title: The brainstorming override becomes a vendor link and an AGENTS.md preference (#62)
date: 2026-10-22
status: per #62's hand-off from epic #24 (2026-10-21)
---

# The brainstorming override becomes a vendor link

## Problem

`skills/brainstorming/` is a v6.4.2 copy (`8ca22db`), kept to change one path. The vendor is at v7 (`b739a8a`): `SKILL.md` rewritten, `spec-document-reviewer-prompt.md` gone, `builder-check-prompt.md` added and unlinked. The doctor reports the drift. v7 says "Your human partner's preferences override both" for where the design goes, so a stated preference does the fork's job with no re-sync.

## Approach

- `skills/brainstorming` → symlink `../skills-vendor/obra-superpowers/skills/brainstorming`. `.claude/skills/brainstorming` unchanged.
- AGENTS.md, a `### Brainstorming` preference under Development Methodology:
  - a change to the service's behaviour amends the spec of record (a dated **Amended** entry plus the sections it touches), as #29–#57 did;
  - a new design doc only for work the spec doesn't cover: `docs/specs/YYYY-MM-DD-<issue>-<topic>-design.md`, never `docs/superpowers/specs/`. Issue first, as plans; `-design` kept, as the spec, the override and v7;
  - `superpowers:<name>` means this repo's `skills/<name>`: `superpowers:writing-plans` is gregoryfoster's `writing-plans`, writing to `docs/plans/`; `superpowers:using-git-worktrees` likewise;
  - the builder check runs as written; on an amendment, the builder reads the spec's amended sections.
- docs/SKILLS.md: obra count 11 + override → 12, `brainstorming` in the list; § Overrides removed (its only row) but the rule kept in one line, so a future override still has a home; line 43's naming note points at AGENTS.md.
- Stale mentions: AGENTS.md Detail Docs and § Agent Skills, `.socraticodeignore:6`, `.socraticodecontextartifacts.json:21`, docs/SOCRATICODE.md:203, `tests/test_socraticode_config.py:74`. `tests/test_drift.py:115` stays: `skills/brainstorming` is still a non-runtime path.

## Test

`tests/test_skills.py`:
- `test_skills_md_inventory_matches_the_links` checks the obra count too (it skipped it for the "+ override" heading). Red now: the heading says 11, the list names 12.
- New: every `superpowers:<name>` a linked superpowers skill's `SKILL.md` invokes resolves to `skills/<name>`, and where that entry is not superpowers', AGENTS.md names `` `superpowers:<name>` ``. Red now: AGENTS.md names neither.
- `test_skills_are_vendor_links_or_declared_overrides` and `test_override_links_resolve` stay, vacuous until the next override.

Known limit: the new test reads vendor text, so an upstream bump that invokes an unlinked skill fails CI on `main` after the hook's push. That is the signal wanted, at the same place `SKILL.md is_file` already fails.

## Ship

Doctor before/after in the PR; `git ls-files -s skills/brainstorming` shows `120000`. PR (`Closes #62`) → `git pull --ff-only` → FF merge → `git pull --ff-only`. No deploy.

## Open questions

None.
