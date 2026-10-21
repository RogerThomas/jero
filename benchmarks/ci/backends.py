"""Shared client configuration and database setup, outside the measured requests."""

import os
from dataclasses import dataclass
from datetime import timedelta
from typing import Self

from psycopg import AsyncConnection
from psycopg.rows import class_row
from psycopg_pool import AsyncConnectionPool
from pyreqwest.client import Client, ClientBuilder

from benchmarks.ci.common import Profile, Workload


@dataclass(frozen=True)
class Settings:
    """Local benchmark services; these credentials are public test data."""

    database_dsn: str = "postgresql://benchmark:benchmark@127.0.0.1:5432/benchmark"
    upstream_url: str = "http://127.0.0.1:8001"

    @classmethod
    def from_environment(cls) -> Self:
        """Read settings once when each server starts."""
        defaults = cls()
        return cls(
            database_dsn=os.environ.get("BENCHMARK_DATABASE_DSN", defaults.database_dsn),
            upstream_url=os.environ.get("BENCHMARK_UPSTREAM_URL", defaults.upstream_url),
        )

    def client(self) -> Client:
        """Use identical HTTP settings and connection reuse for both frameworks."""
        return (
            ClientBuilder()
            .base_url(self.upstream_url)
            .default_headers({"authorization": f"Bearer {Workload.upstream_token()}"})
            .error_for_status()
            .connect_timeout(timedelta(seconds=1))
            .timeout(timedelta(seconds=5))
            .follow_redirects(enable=False)
            .no_proxy()
            .build()
        )

    def pool[T](self, model: type[T]) -> AsyncConnectionPool[AsyncConnection[T]]:
        """Use the same fixed pool size and construct each framework's typed model."""
        return AsyncConnectionPool(
            self.database_dsn,
            min_size=8,
            max_size=8,
            kwargs={"row_factory": class_row(model), "autocommit": True},
            open=False,
        )


async def seed_database(settings: Settings) -> None:
    """Seed one known profile before warmup; measured requests perform real SELECTs."""
    async with settings.pool(Profile) as pool:
        await pool.wait(timeout=15)
        async with pool.connection() as connection:
            await connection.execute(
                "CREATE TABLE IF NOT EXISTS benchmark_users ("
                "id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL, "
                "address TEXT NOT NULL, city TEXT NOT NULL, country TEXT NOT NULL)"
            )
            profile = Workload.profile()
            await connection.execute(
                "INSERT INTO benchmark_users (id, name, email, address, city, country) "
                "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO UPDATE SET "
                "name = EXCLUDED.name, email = EXCLUDED.email, address = EXCLUDED.address, "
                "city = EXCLUDED.city, country = EXCLUDED.country",
                (
                    profile.id,
                    profile.name,
                    profile.email,
                    profile.address,
                    profile.city,
                    profile.country,
                ),
            )
