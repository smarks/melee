# Melee — Claude Code notes

Melee is a Django implementation of *The Fantasy Trip: Melee* — pure-Python rules in
`engine/`, a thin JSON/web layer in `board/`. Broader conventions live in
`~/obsidian-nexus/CLAUDE.md`; the rule below is repeated here so a Claude working in
this repo always loads it.

## Multi-Claude issue coordination (REQUIRED)

More than one Claude works this repo's GitHub issues at the same time. Collisions —
two Claudes building the same issue (e.g. the #82 double-build), or one stepping on
another's in-progress work — happen when claims aren't visible. Before starting **any**
issue:

1. **Check it's free.** Skip it if it has an assignee (anyone's) **or** a claim comment
   without a later "done / stopping" note.
2. **Claim it before you touch code:** both
   - `gh issue edit <N> --add-assignee @me`, **and**
   - a comment: `🤖 claude-<session-short-id> is working on this now (started <date>).`

   The assignee makes claimed issues obvious in the list; the comment says *which* Claude
   (GitHub has no per-Claude user). Use a stable handle per session (first 8 chars of the
   session id).
3. **If you stop without finishing**, unassign yourself and leave a follow-up comment
   releasing it.
4. **When done**, the PR's `Closes #N` closes the issue.

This only works if **every** Claude does it. If you find an issue you're clearly meant to
work but it's unclaimed, claim it first, then proceed.
