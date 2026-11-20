# Agent instructions

Work directly on main. Do not create or use branches for changes in this repository.

## Keep agent code split into modules

Do not write huge single-file agents. Very large brains fail conformance and time out during deploy.

- Keep `asst/<agent>/agent/agent.py` small: the `main` entry point and message routing only.
- Put the rest in separate modules in the same `agent/` folder, each under about 400 lines. Prefix module names with the agent (for example `bc_prefs.py`, `hosp_checkin.py`), so tests that load several agents in one process do not collide.
- The SDK loads `agent/agent.py` as a standalone module, so relative imports do not work. At the top of `agent.py`, add `sys.path.insert(0, str(Path(__file__).parent))`, then import the modules by name.
- The Dockerfile copies the whole `agent/` folder, so new modules ship with the image.
- Never commit a file that still contains merge-conflict markers. Run `python -m py_compile` on the agent and the unit tests before every push.

## Keep agent code split into modules

Do not write huge single-file agents. Very large brains fail conformance and time out during deploy.

- Keep `asst/<agent>/agent/agent.py` small: the `main` entry point and message routing only.
- Put the rest in separate modules in the same `agent/` folder, each under about 400 lines. Prefix module names with the agent (for example `bc_prefs.py`, `hosp_checkin.py`), so tests that load several agents in one process do not collide.
- The SDK loads `agent/agent.py` as a standalone module, so relative imports do not work. At the top of `agent.py`, add `sys.path.insert(0, str(Path(__file__).parent))`, then import the modules by name.
- The Dockerfile copies the whole `agent/` folder, so new modules ship with the image.
- Never commit a file that still contains merge-conflict markers. Run `python -m py_compile` on the agent and the unit tests before every push.
