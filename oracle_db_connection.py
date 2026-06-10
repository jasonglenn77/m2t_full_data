# oracle_db_connection.py
"""
Read Oracle credentials from environment variables.

Set on the machine that runs save_daily_data.py:

    [Environment]::SetEnvironmentVariable("C2M_USER",     "<username>", "User")
    [Environment]::SetEnvironmentVariable("C2M_PASSWORD", "<password>", "User")
    [Environment]::SetEnvironmentVariable("LG_USER",      "<username>", "User")
    [Environment]::SetEnvironmentVariable("LG_PASSWORD",  "<password>", "User")

Restart PowerShell after setting. Optional overrides:
    C2M_HOST, C2M_PORT, C2M_SERVICE, LG_TNS_ADMIN, LG_DSN
"""

import os

try:
    import oracledb as cx_Oracle
except ImportError:
    import cx_Oracle  # fallback if oracledb isn't available

_THICK_INIT = False


def _ensure_thick_mode():
    """Initialize thick-mode Oracle client (required for Native Network Encryption)."""
    global _THICK_INIT
    if _THICK_INIT:
        return
    try:
        cx_Oracle.init_oracle_client()
    except Exception:
        # Fall back to a default Instant Client path if not on PATH
        cx_Oracle.init_oracle_client(lib_dir=r"C:\oracle\instantclient_19_23")
    _THICK_INIT = True


def _require_env(*names):
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise RuntimeError(
            "Missing required environment variable(s): "
            + ", ".join(missing)
            + ". See README.md (Database credentials) for setup."
        )


def get_c2m_connection():
    _ensure_thick_mode()
    _require_env("C2M_USER", "C2M_PASSWORD")

    username = os.environ["C2M_USER"]
    password = os.environ["C2M_PASSWORD"]
    host = os.environ.get("C2M_HOST", "prod-edr-scan.csu.org")
    port = os.environ.get("C2M_PORT", "1521")
    service_name = os.environ.get("C2M_SERVICE", "psc2mrpt.world")

    dsn = f"{host}:{port}/{service_name}"
    return cx_Oracle.connect(user=username, password=password, dsn=dsn)


def get_lg_connection():
    _ensure_thick_mode()
    _require_env("LG_USER", "LG_PASSWORD")

    os.environ["TNS_ADMIN"] = os.environ.get(
        "LG_TNS_ADMIN", r"C:\Users\jsglenn\Documents\WALLET"
    )
    username = os.environ["LG_USER"]
    password = os.environ["LG_PASSWORD"]
    dsn = os.environ.get("LG_DSN", "CSUCCDIRECT")

    return cx_Oracle.connect(user=username, password=password, dsn=dsn)


if __name__ == "__main__":
    try:
        conn = get_c2m_connection()
        print("Connection successful.")
        cur = conn.cursor()
        cur.execute("SELECT 'Hello from Oracle!' FROM dual")
        for row in cur:
            print(row[0])
        cur.close()
        conn.close()
    except Exception as e:
        print("Connection failed:", e)
