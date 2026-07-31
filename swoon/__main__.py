"""Allow ``python -m swoon`` to run the CLI."""

from .cli import main


raise SystemExit(main())
