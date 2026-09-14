# Contributing

Thanks for your interest in contributing to Vesperiki! Issues and pull requests are welcome.

## Development setup

Backend:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
pytest                                # backend test suite
```

Frontend (requires [pnpm](https://pnpm.io/)):

```bash
cd frontend
pnpm install
pnpm test                             # vitest + jsdom + Testing Library
pnpm typecheck                        # tsc --noEmit, strict tsconfig
pnpm build                            # emits dist/
```

## Where tests live

- Backend: `tests/`, run by pytest.
- Frontend: colocated next to the code they exercise — `src/lib/*.test.ts`, `src/components/*.test.tsx` — run by vitest.

## Code style

- Python: typed functions throughout; public domain logic lives in `vesperiki/service.py` and returns plain dicts.
- TypeScript: strict mode; keep `pnpm typecheck` clean.
- Match the style of the surrounding code; avoid drive-by reformatting.

## Opening a pull request

1. Fork the repository and create a feature branch.
2. Make your changes in focused commits.
3. Run **both** test suites before submitting:
   - `pytest` at the repo root
   - `pnpm test` inside `frontend/`
4. Open a pull request describing what changed and why, linking any related issue.
