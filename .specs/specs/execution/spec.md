# Execution Specification

> Generated from design document analysis on 2026-04-04
> Source files: docs/specs/2026-04-04-ai-vfs-design.md (Sections 8, 3.4, 3.5)

## Purpose

Sandboxed code execution with VFS operations injected as callbacks.
Execution providers run agent-generated code (bash, Python) in isolation, with no direct access to storage.
A shell operations layer translates bash command signatures into VFS operations.

## Requirements

### Requirement: PluggableExecutionProviders

The system SHALL support pluggable execution providers, each declaring which languages it supports.
Initial providers: Bashkit (bash subset) and Monty (Python subset).
Future providers (Eryx, PyMiniRacer, E2B) SHALL integrate without core changes.

#### Scenario: ExecuteWithBashkit

- **GIVEN** a Bashkit execution provider is configured
- **WHEN** an agent submits bash code
- **THEN** the code executes in the Bashkit sandbox with VFS operations available

#### Scenario: ProviderCapabilities

- **GIVEN** a Bashkit provider and a Monty provider
- **WHEN** capabilities are queried
- **THEN** Bashkit declares {"bash"} and Monty declares {"python"}

### Requirement: FunctionInjection

The system SHALL inject VFS operations into the sandbox as callable functions (the function-injection pattern).
The sandbox SHALL NOT access storage directly.

#### Scenario: InjectedReadCallback

- **GIVEN** a sandbox with injected fs_ops
- **WHEN** sandboxed code calls read("/workspace/file.txt")
- **THEN** the call routes through the VFS orchestrator (permission check, metadata lookup, blob fetch)

#### Scenario: NoDirectStorageAccess

- **GIVEN** a sandbox executing agent code
- **WHEN** the code attempts to access the host filesystem or storage directly
- **THEN** the access is blocked (sandbox has no I/O by default)

### Requirement: ShellOperationsLayer

The system SHALL provide shell operation wrappers that translate standard
bash command signatures into VFS operations: grep, find, glob, cat, ls,
head, tail.

#### Scenario: GrepTranslation

- **GIVEN** sandboxed code calls grep("-r", "TODO", "/workspace/")
- **WHEN** the shell ops layer processes the call
- **THEN** it dispatches to vfs.search(query="TODO", scope="/workspace/", type=regex)

#### Scenario: CatTranslation

- **GIVEN** sandboxed code calls cat("/workspace/file.txt")
- **WHEN** the shell ops layer processes the call
- **THEN** it dispatches to vfs.read("/workspace/file.txt")

### Requirement: PermissionScopedCallbacks

The system SHALL construct FsOperations callbacks bound to the calling principal and namespace.
The sandbox can only access paths the principal is authorized for.

#### Scenario: SandboxPermissionEnforced

- **GIVEN** a principal with read-only access to /workspace/
- **WHEN** sandboxed code attempts to write via the injected write callback
- **THEN** a PermissionDeniedError is raised

### Requirement: ResourceLimits

The system SHALL enforce resource limits on sandboxed execution at two levels:
VFS-level max_operations (caps VFS callbacks per execution) and provider-level
limits (timeout, memory, fuel).

#### Scenario: MaxOperationsExceeded

- **GIVEN** max_operations=100 configured
- **WHEN** sandboxed code makes 101 VFS read calls
- **THEN** the 101st call is denied and execution terminates

#### Scenario: TimeoutEnforced

- **GIVEN** timeout_seconds=5.0 configured
- **WHEN** sandboxed code runs for 6 seconds
- **THEN** execution is terminated by the provider

### Requirement: StatelessExecution

The system SHALL execute each call in a fresh sandbox by default.
State persistence (snapshot/restore) MAY be added as optional protocol methods in a future version.

#### Scenario: FreshSandboxPerCall

- **GIVEN** a previous execution wrote variables in the sandbox
- **WHEN** a new execute call is made
- **THEN** the sandbox starts clean with no state from the previous call

## Technical Notes

- **Implementation**: src/aifs/protocols/execution.py (protocol), shell ops layer (Phase 3)
- **Dependencies**: file-operations (VFS callbacks), access-control (permission-scoped callbacks), search (grep dispatch)
- **Initial providers**: Bashkit (PyPI: bashkit-python), Monty (PyPI: pydantic-monty) — both Phase 3
