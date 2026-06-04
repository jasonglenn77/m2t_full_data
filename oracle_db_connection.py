# oracle_db_connection.py
import os

# Use oracledb and alias as cx_Oracle for compatibility
try:
    import oracledb as cx_Oracle
except ImportError:
    import cx_Oracle  # fallback if oracledb isn't available

# ---- Initialize THICK mode ----
# This allows Native Network Encryption (NNE)
_THICK_INIT = False
def _ensure_thick_mode():
    global _THICK_INIT
    if _THICK_INIT:
        return
    try:
        # If your Instant Client / Oracle client is already on PATH, this is enough:
        cx_Oracle.init_oracle_client()
    except Exception:
        # Otherwise, point directly to your Instant Client folder:
        # TODO: replace this path with your actual Instant Client directory
        cx_Oracle.init_oracle_client(lib_dir=r"C:\oracle\instantclient_19_23")
    _THICK_INIT = True

def get_c2m_connection():
    _ensure_thick_mode()

    username = "jsglenn"
    # username = "PRZAUT"
    password = "BuffaloBills777$"
    # password = "RickyBobby$Talladega7"
    host = "prod-edr-scan.csu.org"
    port = 1521
    service_name = "psc2mrpt.world"

    dsn = f"{host}:{port}/{service_name}"  # thin-style DSN is fine; we're in thick mode now
    conn = cx_Oracle.connect(user=username, password=password, dsn=dsn)
    return conn

def get_lg_connection():
    _ensure_thick_mode()

    os.environ["TNS_ADMIN"] = r"C:\Users\jsglenn\Documents\WALLET"
    username = "CSU_JSGLENN"
    password = "JSGLENN#20260129#direct"
    dsn = "CSUCCDIRECT"
    conn = cx_Oracle.connect(user=username, password=password, dsn=dsn)
    return conn

if __name__ == "__main__":
    try:
        conn = get_c2m_connection()  # or get_lg_connection()
        print("✅ Connection successful!")
        cur = conn.cursor()
        cur.execute("SELECT 'Hello from Oracle!' FROM dual")
        for row in cur:
            print(row[0])
        cur.close()
        conn.close()
    except Exception as e:
        print("❌ Connection failed:", e)