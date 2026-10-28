"""MySQL connection helpers for the Construction Management System."""

import os
from contextlib import contextmanager

import mysql.connector
from mysql.connector import MySQLConnection


def get_db_connection() -> MySQLConnection:
    """Open a MySQL connection using environment-based configuration."""
    return mysql.connector.connect(
        host=os.getenv("MYSQL_HOST", "localhost"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER", "root"),
        password=os.getenv("MYSQL_PASSWORD", ""),
        database=os.getenv("MYSQL_DATABASE", "construction_management"),
    )


@contextmanager
def db_cursor(dictionary: bool = True):
    """Yield a cursor and commit on success, rolling back on database errors."""
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=dictionary)
    try:
        yield cursor
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()
        connection.close()
