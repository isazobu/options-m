"""One module per autonomous agent.

Each agent implements the :class:`~options_m.agents.Agent` protocol: a `name`,
an optional `interval_seconds`, and a `step()` that performs exactly one
iteration. Looping, pacing and error backoff belong to the supervisor in
``agents.py`` — an agent that catches its own errors hides them from it.
"""
