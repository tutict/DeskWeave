# deskweave-contracts

Shared, dependency-light data contracts used across the DeskWeave runtime, SDKs,
adapters and the MCP gateway.

This package intentionally depends only on `pydantic`. It must not import the
runtime, the SDKs, adapters or the MCP gateway, so that every other component can
depend on it without creating cycles.
