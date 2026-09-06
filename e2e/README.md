# End-to-end UI tests (Playwright)

These tests drive the **real board SPA in a real browser** against a live Django
server, so the whole stack is exercised together: the template, the inline-JS
SPA, the JSON API, the rules engine, and the AI.

They are **not** part of the default `pytest` run (`pytest.ini` limits
`testpaths` to `engine/tests` and `board/tests`), so CI and the deploy gate stay
browser-free. Run them explicitly.

## Setup (once)

```bash
pip install pytest-playwright      # also pulls in pytest-django (already a dep)
python -m playwright install chromium
```

## Run

```bash
pytest e2e/                          # headless; records video to e2e/videos/
pytest e2e/ --headed --slowmo 400    # WATCH a match play out, slowed down
pytest e2e/ -k full_game --headed    # just the full-game playthrough, live
```

Every run records a `.webm` per test under `e2e/videos/`, so even a headless CI
run leaves a watchable artifact of the match.

## What's covered

- **`test_full_game.py`** — loads the board (which auto-boots a Player-vs-Computer
  match), then advances the turns through the game's own controls while the AI
  plays its side, until one side *wins the field*. Drives the entire stack and
  exercises the AI's movement/combat decisions and most of the action API.
- **`test_interactions.py`** — the human-control entry points: starting a game
  from the setup dialog (same screen), and rolling initiative through to the
  movement phase.

## Locators: pin buttons by id when a name can be a substring of another

Playwright matches an accessible name **case-insensitively, as a substring**, so
`get_by_role("button", name="New Game")` matches Game Control's `#newGameBtn`
*and* the victory panel's `New game →` (rendered by `drawControls` in
`board.js` once a side wins). Both are on screen whenever a match is decided,
and the locator then dies on a strict-mode violation, "resolved to 2 elements".

That is the #436 flake: a seeded broadsword exchange sometimes downs a fighter,
the match ends, the victory button renders, and a test that had passed a hundred
times reddens `main`. It is timing-shaped only because the roll is random.

So drive Game Control's button as `page.locator("#newGameBtn")`, and apply the
same rule to any other label that is a prefix of a longer one. Reach for
`exact=True` or an id whenever a role name could grow a suffix.

## Coverage

Because `live_server` runs in the same process as the test, `coverage` captures
the server-side code the browser hits. To see the combined picture:

```bash
pytest engine/tests board/tests e2e/ --cov=engine --cov=board --cov-report=term-missing
```
