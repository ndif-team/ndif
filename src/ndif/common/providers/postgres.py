import os
import logging

import psycopg2
import psycopg2.pool

from . import Provider, retry

logger = logging.getLogger("ndif")


class PostgresProvider(Provider):
    host: str
    port: str
    database: str
    user: str
    password: str
    pool: psycopg2.pool.ThreadedConnectionPool = None
    min_connections: int = 1
    max_connections: int = 10

    @classmethod
    def from_env(cls) -> None:
        super().from_env()
        cls.host = os.environ.get("POSTGRES_HOST", "localhost")
        cls.port = os.environ.get("POSTGRES_PORT", "5432")
        cls.database = os.environ.get("POSTGRES_DB", "ndif")
        cls.user = os.environ.get("POSTGRES_USER", "")
        cls.password = os.environ.get("POSTGRES_PASSWORD", "")
        cls.min_connections = int(os.environ.get("POSTGRES_MIN_CONNECTIONS", "1"))
        cls.max_connections = int(os.environ.get("POSTGRES_MAX_CONNECTIONS", "10"))

    @classmethod
    def to_env(cls) -> dict:
        return {
            **super().to_env(),
            "POSTGRES_HOST": cls.host,
            "POSTGRES_PORT": cls.port,
            "POSTGRES_DB": cls.database,
            "POSTGRES_USER": cls.user,
            "POSTGRES_PASSWORD": cls.password,
        }

    @classmethod
    @retry
    def connect(cls) -> None:
        logger.info(f"Connecting to PostgreSQL at {cls.host}:{cls.port}/{cls.database}...")
        cls.pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=cls.min_connections,
            maxconn=cls.max_connections,
            host=cls.host,
            port=cls.port,
            database=cls.database,
            user=cls.user,
            password=cls.password,
            connect_timeout=10,
        )
        logger.info("Connected to PostgreSQL")

    @classmethod
    def connected(cls) -> bool:
        if cls.pool is None or cls.pool.closed:
            return False
        conn = None
        try:
            conn = cls.pool.getconn()
            conn.isolation_level
            return True
        except Exception:
            return False
        finally:
            if conn is not None:
                try:
                    cls.pool.putconn(conn)
                except Exception:
                    pass

    @classmethod
    def reset(cls) -> None:
        if cls.pool is not None:
            try:
                cls.pool.closeall()
            except Exception:
                pass
            cls.pool = None

    @classmethod
    @retry
    def execute(cls, query: str, params: tuple = None, fetchone: bool = False):
        conn = cls.pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(query, params)
                if fetchone:
                    return cur.fetchone()
                return cur.fetchall()
        except Exception:
            conn.rollback()
            raise
        finally:
            cls.pool.putconn(conn)
