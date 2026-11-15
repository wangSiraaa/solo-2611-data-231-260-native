"""Allow ``python -m evjoin`` as a shorthand for the CLI."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
