# deskweave-plugin-sdk

Contract for DeskWeave plugins. A plugin packages cross-application workflow
logic (for example `anime-modeling`). Plugins receive a restricted
`PluginContext`:

- they may register adapters and workflow definitions;
- they may **not** touch the core database tables directly;
- they may **not** bypass the permission layer.

Phase 0 ships the manifest + setup contract only; a plugin marketplace and
dynamic loading are explicitly out of scope.
