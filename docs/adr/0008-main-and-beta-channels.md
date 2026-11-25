# ADR-0008: Main and beta release channels from two branches

## Status
Accepted (2026-11)

## Context
New features need trying out on a real account (including the owner's own server) before everyone gets them, but
the app updates itself from GitHub, so anything pushed to `main` reaches every user.

## Decision
- Two branches: `main` (the release) and `beta` (features being tried). Settings → Updates → Release channel picks
  which one the app follows; the default is main. The installed channel is remembered in `.flip-channel` in the app
  folder, so a "BETA" badge shows in the top bar and a switch is offered when the choice and the code differ.
- Beta versions are `X.Y.Z-beta.N`. Version comparison puts `1.8.0` above `1.8.0-beta.N`, so beta users rejoin the
  release automatically when a beta is merged.
- Switching channel installs that branch's latest code even when its version number is lower; the database is
  backed up first. Git installs fetch and check out the branch; zip installs download that branch's zip.

## Alternatives considered
- **Feature flags in one build:** every half-finished feature would ship to everyone, switched off; more code paths
  to keep working.
- **GitHub pre-releases/tags:** more steps for each release than pushing to a branch.

## Consequences
- Schema changes must stay additive (new tables/columns), so going back from beta to main is safe.
- CI runs on both branches.
