"""Intentionally incomplete fixture: no HTTP server, port or health route."""
import os


def main():
    if not os.environ.get("DATABASE_URL"):
        raise RuntimeError("DATABASE_URL must be configured")
    raise NotImplementedError("Application startup is not implemented")


if __name__ == "__main__":
    main()
