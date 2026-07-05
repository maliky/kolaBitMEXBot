# AGENT - Codex Playbook

## 1. Quick Repository Map
- `kolabi/bot/`: active strategy runtime and CLI. Start here for order-pair behaviour, Org strategy parsing, head/tail lifecycle, repeats, hooks, tail tracking, and reports.
- `kolabi/bot/domain.py`, `pair_cycle.py`, `isis.py`, `chronos.py`, `horus.py`, `ogun_executor.py`, `strategy_runtime.py`: current runtime core. Keep pure state transitions separate from scheduling and exchange side effects.
- `kolabi/tree/`: DB-backed public market feeders and private account/order feeders for Kraken, Binance, and BitMEX.
- `kolabi/shared/`: shared exchange adapters, route metadata, runtime command types, persistence models, DB helpers, logging, and quantity conversion.
- `kolabi/bargain/`: direct operator CLI and smoke-test surface for exchange actions outside the strategy runtime.
- `orders/*.org`: active strategy source of truth. `orders/*.tsv` files may exist for older or compatibility examples, but do not assume TSV is the active grammar.
- `scripts/kolabidb`, `scripts/kolabi-fresh-run`, `scripts/kolabi-run-report`: local service, fresh-run, and operator-report helpers.
- `archives/` and `kolabi/runtime/kola/`: historical or transitional reference only. Do not build new behaviour there unless the user explicitly asks.
- `tests/bot/`, `tests/core/`, `tests/tree/`, `tests/exchanges/`, `tests/scripts/`: active regression surface.

## 2. Current Architecture Boundaries
1. Keep pair lifecycle logic as typed state/event work first: `State + Event -> State + Commands`.
2. Put pure transition rules in `pair_cycle.py`, `isis.py`, domain helpers, or focused runtime-policy helpers. These functions should not call exchange clients or mutate external state.
3. Keep `Chronos` responsible for ordering, deduplication, dependency activation, repeats, and runtime event orchestration.
4. Keep `Horus` responsible for translating reducer intents into runtime commands.
5. Keep `Ogun` and exchange adapters responsible for irreversible side effects: REST calls, cancel/amend/place execution, and adapter payload conversion.
6. Keep `StrategyRuntime` as coordinator glue. It may feed events, query DB evidence, emit telemetry, and hand commands to Ogun, but avoid hiding pair semantics inside it when a pure transition can own them.
7. Keep DB feeders in `kolabi/tree/`. Strategy code should consume persisted market/private evidence rather than opening raw websocket feeds directly.

## 3. Strategy Grammar And Runtime Semantics
- Active strategy files live under `orders/` and are normally Org tables.
- The active Org header is:

```text
| exchg | symbol | name | tps_run | essais | tOut | pause | cool | side | oType | hDelta | qty | tType | tDelta | pGate | hPrice | tPrice | tUblk | wUblk | hook |
```

- `exchg` and `symbol` are optional per row. When absent, CLI defaults are used.
- Route codes include Kraken `KRKF`, `KRKS`, `KRKM`; Binance `BINF`, `BINS`, `BINM`, `BINI`; BitMEX `BTXF`, `BTXS`, with older aliases still present in compatibility paths.
- Typed values use explicit prefixes where required: `D`, `%`, `A`, or `U`. Do not silently accept untyped non-empty values unless the parser already does.
- `qty=U...` means USD notional and is converted internally from market evidence and instrument metadata. Base quantity still has to respect exchange minimums and increments.
- `tps_run` controls when a pair attempt may start; it does not cancel an already-started attempt.
- `tOut` applies after a head is sent or acknowledged, not while a pair is still latent behind time, chain, or price gates.
- `pause` controls repeat spacing. `cool` is added only after a successful tail close.
- Tail updates are no-widen by design. Do not emit or accept an amend that worsens the protected stop unless the user explicitly changes that invariant.

## 4. DB, Services, And Operator Commands
- PostgreSQL is the supported local runtime backend. Use `.env.postgres` and `scripts/kolabidb postgres start`.
- A live strategy needs fresh public market data and private account/order/fill evidence for every active route in the strategy.
- `scripts/kolabi-fresh-run` is the normal pre-run helper. It stops local Kolabi feeder PID files, starts PostgreSQL, optionally purges Kolabi runtime DB rows, starts needed public/private feeders, and prints the bot command. It does not cancel platform orders or close positions.
- `scripts/kolabi-run-report` reads logs and local DB evidence to produce the Org operator report. Use `--log-only` when DB access is unavailable, and state that limitation clearly.
- `python -m kolabi.bot run --strategy orders/demo_ada.org --exchange kraken --market-type futures --symbol PI_XBTUSD --environment demo --dry-run` is the basic dry-run shape.
- `python -m kolabi.bot preflight ...` checks route credential and DB readiness before a run.
- `python -m kolabi.bargain.cli ...` is for direct operator actions such as `routes`, `instruments`, `check-symbol`, `balance`, `open-orders`, `cancel`, and `close-all`.
- Do not send platform cleanup commands after the user says they already cancelled orders manually, unless they explicitly ask for another exchange-side action.

## 5. Environment And Tooling
- Python version is forced by `.python-version` (`kola`). If pyenv lacks it, use `PYENV_VERSION=system` only as a temporary workaround and report it.
- Install runtime dependencies from `requirements.txt` and developer tools from `requirements-dev.txt`. The current stack uses SQLAlchemy, psycopg, pandas, numpy, python-binance, responses, pytest, mypy, pyright, ruff, and black.
- `run.sh` is not the active service launcher. Prefer `scripts/kolabi-fresh-run`, `scripts/kolabidb`, and `python -m kolabi.bot ...`.
- Logs live under `logs/`. Large feeder logs and local PostgreSQL failures can be operationally relevant; do not assume a stopped bot is a strategy bug before checking log timestamps, disk space, and feeder status.
- Secrets must come from environment variables or local env files. Do not print raw credential-bearing DB URLs or API keys.

## 6. Tests And Validation
- Before editing, check `git status --short` and preserve user changes.
- For parser or strategy grammar work, run focused tests such as:
  - `pytest tests/bot/test_tsv_parser.py tests/bot/test_cli.py -q`
  - `pytest tests/bot/test_strategy_runtime.py tests/bot/test_pair_cycle_step.py -q`
- For runtime boundary work, run the smallest relevant set from `tests/bot/` and `tests/core/`, then broaden if shared state, commands, or persistence changed.
- For feeder, DB, or adapter work, run targeted tests from `tests/tree/`, `tests/exchanges/`, `tests/shared/`, and `tests/scripts/`.
- For report changes, run `pytest tests/bot/test_run_report.py -q` and verify rendered Org sections rather than brittle whole-line spacing.
- Use `python -m ruff check kolabi tests` and type checks when the change touches shared typed interfaces.

## 7. Editing Rules
- Keep comments bilingual when touching legacy sections that already use mixed French/English comments.
- Use British English as the working language for code, docs, and reports unless the user explicitly asks otherwise.
- Preserve the existing Org formatting style in repo docs such as `README.org`, `DEV.org`, and `MANUAL.org`; extend the local pattern instead of reformatting the whole file.
- For `.org` documentation, do not hard-wrap lines at 80 columns; keep original long lines and let the user manage wrapping.
- Treat comment lines starting with `# >` in Org files as operator notes for Codex; satisfy them when the surrounding task touches that section, and then remove or replace them with the concrete result.
- Final summaries for the user should be formatted as org-mode bullets/headings done with `-` and simple `*`.
- Avoid non-legally-safe glyphs. Use plain ASCII punctuation and symbols.
- When unsure, inspect `README.org`, `DEV.org`, `MANUAL.org`, and `done.org` for current intent before deleting or refactoring logic.

## 8. Metaphors And Naming
This codebase intentionally uses human-level metaphors. Do not mechanically replace them with literal names.

Metaphors are allowed when they name a black box for human complexity, orchestration, market behaviour, or strategic agency. Literal names are preferred for typed states, payloads, events, commands, and pure transition functions.

Keep intentional metaphors: `Chronos`, `Bargain`, `Dragon`, `head`, `tail`, `hook`, `flying`, `flapping`, `market`, `MarketAuditor`, `Horus`, `Ogun`, `Isis`.

Keep Isis narrow: it consumes already targeted strategy events, updates or replaces `StrategyState`, delegates pair lifecycle semantics to `step_pair()`, and emits ordered intents only. Pair-name resolution, deduplication, precedence, pending-identity timeout, dependency activation, `RuntimeCommand` translation, and exchange execution belong outside Isis, mainly to Chronos, Horus, or Ogun.

Welcome, let's Swing and Jazz!
