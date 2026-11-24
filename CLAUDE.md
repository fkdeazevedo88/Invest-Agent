# Invest-Agent

Personal investing agents for a Canadian retail investor using moomoo (Moomoo Financial Canada)
through the OpenD gateway on their own computer. Explain things to the owner in simple, plain English.

- **ETF agent** (`run.py`, `config.yaml`): diversified ETFs, real money only with `--live`.
- **High-risk agent** (`run.py --config config-highrisk.yaml`): individual stocks and CDRs.
- **Research agent** (`research.py`, `config-research.yaml`): one US stock at a time, moomoo PAPER
  trading only. When the owner types **"run the research"**, follow
  `.claude/skills/run-the-research/SKILL.md` exactly.

Rules for any change or run:
- Never place real-money orders unless the owner explicitly asks for that specific run. The research
  agent is paper only, with no real-money option, and must stay that way.
- Never edit the agents' records (`state/`, `state-highrisk/`, `state-research/`) by hand.
- Safety rules are code, not advice: no borrowing, no short selling, only sell what an agent bought,
  one run at a time (`run.lock`). Keep them, and keep their tests passing:
  `python -m unittest discover -s tests`.
