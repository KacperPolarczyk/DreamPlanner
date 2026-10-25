"""Interactive CLI for creating DreamPlanner's first administrator."""

import argparse
import getpass
import warnings

from src.core.database import get_session_factory
from src.users.service import (
    ActiveAdministratorExists,
    BootstrapError,
    BootstrapValidationError,
    bootstrap_administrator,
    validate_bootstrap_account,
)


def main(argv: list[str] | None = None) -> int:
    """Read the password privately and run the bootstrap workflow.

    Args:
        argv: Optional CLI arguments for invocation or tests.

    Returns:
        Process exit status, zero on success.
    """
    parser = argparse.ArgumentParser(
        description="Create the first administrator", allow_abbrev=False
    )
    parser.add_argument("--email", required=True)
    parser.add_argument("--full-name", required=True)
    args, unknown = parser.parse_known_args(argv)
    if unknown:
        parser.exit(2, "Unsupported arguments.\n")

    try:
        validate_bootstrap_account(args.email, args.full_name)
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = getpass.getpass("Administrator password: ")
            confirmation = getpass.getpass("Confirm password: ")
        if password != confirmation:
            raise BootstrapValidationError("Passwords do not match.")
        if not password:
            raise BootstrapValidationError("Password must not be empty.")
        with get_session_factory()() as session:
            bootstrap_administrator(session, args.email, args.full_name, password)
    except (BootstrapValidationError, ActiveAdministratorExists, BootstrapError) as exc:
        parser.exit(1, f"Bootstrap failed: {exc}\n")
    except (EOFError, KeyboardInterrupt, getpass.GetPassWarning):
        parser.exit(1, "Bootstrap cancelled.\n")
    except Exception:
        parser.exit(1, "Bootstrap failed: operation could not be completed.\n")

    print("Administrator created.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
