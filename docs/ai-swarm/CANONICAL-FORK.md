# AI-Swarm canonical Hermes fork

## Source and state boundary

| Concern | Canonical location |
| --- | --- |
| Git source checkout | `/opt/ai-swarm/hermes-agent` |
| Fleet customization branch | `ai-swarm-main` |
| Isolated Python runtime | `~/.hermes/venvs/hermes-ai-swarm` |
| Active CLI link | `~/.local/bin/hermes` → the isolated runtime |
| Profile/config/auth/session state | `~/.hermes` (excluding source checkouts) |
| Pre-migration Git rollback bundle | `~/.hermes/migration-backups/hermes-agent-before-canonical-20260716.bundle` |

Source code must not live under a Hermes profile directory. Runtime state must not be written into the Git checkout.

## Reconciled history

The branch starts at Nous upstream `e0240d7bf7ce0d665417d45de0bfa9a65cb0ab48` and carries the nine existing AI-Swarm commits in original order. A tenth commit aligns managed task lookup with the gateway's `assigned_to` schema. Conflict resolution retained current upstream behavior while preserving:

- Hindsight curation and embedded runtime support;
- LCM context ingestion and the AutoContext plugin;
- credential-free direct HTTP extraction without making Firecrawl the default;
- current upstream plugin-provider discovery;
- launchd's bounded user-domain bootstrap retry plus the AI-Swarm sudo fallback;
- the current TUI `submissionCore` path with managed inbox acknowledgements added as status callbacks;
- quiet macOS dashboard startup.

`origin` is the AI-Swarm fork. The canonical customization branch is pushed as `origin/ai-swarm-main`; upstream reconciliation uses an explicit fetch from `https://github.com/NousResearch/hermes-agent.git` so the source commit is unambiguous.

## Reproduce

```bash
git clone https://github.com/dcmcwhorter/hermes-agent.git /opt/ai-swarm/hermes-agent
cd /opt/ai-swarm/hermes-agent
git switch ai-swarm-main
python3.11 -m venv ~/.hermes/venvs/hermes-ai-swarm
~/.hermes/venvs/hermes-ai-swarm/bin/pip install -e '/opt/ai-swarm/hermes-agent[all,dev]'
env -u PYTHONPATH ~/.hermes/venvs/hermes-ai-swarm/bin/python scripts/ai_swarm_verify_install.py
```

Do not run `hermes setup` during this migration: it may modify profile configuration. Existing `config.yaml`, `auth.json`, session databases, plugins, skills, and memory remain in place.

## Atomic activation

1. Create and verify the Git bundle of the legacy checkout.
2. Verify the canonical venv and focused Python/TUI tests.
3. Atomically replace `~/.local/bin/hermes` with a symlink to `~/.hermes/venvs/hermes-ai-swarm/bin/hermes`.
4. Start a clean process with stale `PYTHONPATH` removed and verify `hermes version` reports `/opt/ai-swarm/hermes-agent`.
5. Rename the legacy source checkout out of `~/.hermes`; do not delete it until independent validation passes.
6. Run `scripts/ai_swarm_verify_install.py --require-active --require-legacy-removed`.

Existing running Hermes processes are not restarted by source activation. They keep their already-imported code until a controlled `--continue` restart.

## Rollback

1. Rename the quarantined legacy checkout back to `~/.hermes/hermes-agent` so its editable venv target exists again.
2. Atomically relink `~/.local/bin/hermes` to the restored legacy venv console script.
3. Start a clean shell and verify `hermes version` reports the legacy commit.
4. If the quarantine was lost, restore the checkout from the Git bundle and reinstall its venv.

No profile/auth/session restoration is needed because migration never moves or rewrites profile state.
