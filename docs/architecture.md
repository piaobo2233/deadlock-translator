# Architecture

This document describes the source tree after the entry-module refactor. Source identity and validation must be recorded with each exported snapshot; the existing 1.0.0 binaries do not contain this refactor.

`python -m app`, `scripts/run.py`, the Windows launcher and frozen executables share `app.main.run()`. The stable entry delegates to `app/bootstrap.py`, which assembles services and owns their startup and shutdown. `app/cli.py` parses arguments and dispatches auxiliary commands before acquiring a translator instance. Windows identity and DPI are initialized explicitly before windows are created.

| Location | Responsibility |
| --- | --- |
| `app/runtime/input_scope.py` | Process input root and data paths; simulation/replay scope is selected before migration, logging or writes |
| `app/config/` | Settings defaults, validation, repository and runtime configuration operations |
| `app/translation/` | Pure text policies, exact authority queries, memory/cache stores, translation and request sharing |
| `app/storage/` | Atomic persistence, locking, review/request/cost records and chat logs |
| `app/recognition/capture.py` | Concrete screen/OCR input adapter; replay injects frames into the same chain |
| `app/recognition/live.py` | Shared current tracking and pause state for capture and rendering |
| `app/runtime/` | Injected source factories, bounded channels, scheduling, message processing and session state |
| `app/presentation/` | Tk-independent layout and display receipt logic |
| `gui/overlay.py`, `gui/session.py` | Tk rendering, display pump and desktop lifecycle |
| `gui/app.py` and page modules | Panel navigation, page lifecycle and existing operation services |

Business and GUI modules do not import `app.main` or `app.bootstrap`. Translation/storage/runtime modules do not import Tk or construct the concrete OCR source. Importing pure policies, stores or capture adapters does not start windows, threads, OCR, network requests or user-data writes.

User data stays under the program's `data/` directory. Simulation and replay use a fresh isolated root without keys or cost ledgers; constructors resolve default paths at construction time. Local lookup order, request recovery fences, manual decisions, lock scopes, cost attribution and message lifetimes are preserved. Unknown request outcomes are never automatically resent.

Only the Tk thread owns controls. Position/version checks and actual draw receipts prevent stale translations from reappearing. Software drawing evidence does not establish game-compositor or recording visibility. pystray and ttkbootstrap remain optional enhancements for source operation.

See [development](DEVELOPMENT.md) for commands and [testing](TESTING.md) for validation scope. Natural matches, complete display/DPI matrices, endurance and clean-system install/upgrade acceptance require separate evidence.
