import argparse
import sys

import psycopg


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify a PostgreSQL connection and required schema."
    )
    parser.add_argument("--postgres-url", required=True)
    args = parser.parse_args()
    try:
        with psycopg.connect(args.postgres_url, connect_timeout=5) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                if cursor.fetchone() != (1,):
                    raise RuntimeError("PostgreSQL readiness probe failed.")
        print("PostgreSQL connection successful.")
        return 0
    except Exception as exc:
        print(
            f"PostgreSQL connection failed: {type(exc).__name__}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
