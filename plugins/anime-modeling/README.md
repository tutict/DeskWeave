# deskweave-plugin-anime-modeling

Example plugin for the DeskWeave reference-image workflow:

1. collect anime-character reference images through the browser connector;
2. let the user confirm a selection;
3. hand the selection to the Blender connector to create reference planes.

Phase 0 ships the plugin *manifest* and a `setup` hook that registers the browser
and Blender adapters. The workflow steps themselves are not implemented yet; the
plugin does not touch the core database.
