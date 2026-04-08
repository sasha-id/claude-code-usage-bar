"""Background update-check entry point.

Called by updater.spawn_update_check_background() as a detached subprocess.
Runs check_and_upgrade() silently — no stdout, no stderr, no side effects
beyond the upgrade itself. Any failure is swallowed: the parent process
has already returned, so there's nobody to report to.
"""

from .updater import check_and_upgrade


def main() -> None:
    try:
        check_and_upgrade()
    except Exception:
        pass


if __name__ == "__main__":
    main()
