# db_oracle.py

try:
    import oracledb as cx_Oracle  # preferred modern driver
except ImportError:
    import cx_Oracle              # fallback if oracledb isn't available

import pandas as pd
from oracle_db_connection import get_c2m_connection
from datetime import datetime
from config import ARRAYSIZE

SQL_HOURLY = """
SELECT
  d1_dvc_identifier.id_value     AS BADGE,
  TRIM(d1_dvc.device_type_cd)    AS DEVICE_TYPE_CD,
  TO_NUMBER(REGEXP_SUBSTR(ci_prem_geo.geo_val, '[^ ]+', 1, 1))        AS BADGE_LAT,
  -TO_NUMBER(REGEXP_SUBSTR(ci_prem_geo.geo_val, '[^ ]+', 1, 2))       AS BADGE_LONG,
  CASE
    WHEN TRIM(d1_dvc.device_type_cd) IN ('1','1D','55') THEN 120
    WHEN TRIM(d1_dvc.device_type_cd) IN ('10N') THEN 208
    WHEN TRIM(d1_dvc.device_type_cd) IN ('5','52','52M','5D','5E','5ED','5EN','5L','5N') THEN 240
    ELSE NULL
  END AS NOMINAL,
  d1_msrmt.msrmt_dttm            AS MSRMTDTTM,
  d1_msrmt.msrmt_val             AS ACTUAL,
  CASE
    WHEN TRIM(d1_dvc.device_type_cd) IN ('1','1D','55') THEN ROUND(d1_msrmt.msrmt_val / 120, 10)
    WHEN TRIM(d1_dvc.device_type_cd) IN ('10N')         THEN ROUND(d1_msrmt.msrmt_val / 208, 10)
    WHEN TRIM(d1_dvc.device_type_cd) IN ('5','52','52M','5D','5E','5ED','5EN','5L','5N') THEN ROUND(d1_msrmt.msrmt_val / 240, 10)
    ELSE NULL
  END AS PUVALUE
FROM cisadm.d1_install_evt
JOIN cisadm.d1_dvc_cfg
  ON d1_install_evt.device_config_id = d1_dvc_cfg.device_config_id
AND d1_install_evt.d1_removal_dttm IS NULL
JOIN cisadm.d1_dvc_cfg_type
  ON d1_dvc_cfg_type.device_config_type_cd = d1_dvc_cfg.device_config_type_cd
AND d1_dvc_cfg_type.d1_svc_type_cd = 'E'
JOIN cisadm.d1_dvc
  ON d1_dvc.d1_device_id = d1_dvc_cfg.d1_device_id
AND TRIM(d1_dvc.device_type_cd) IN ('1','10N','1D','5','52','52M','55','5D','5E','5ED','5EN','5L','5N')
JOIN cisadm.d1_dvc_identifier
  ON d1_dvc_identifier.d1_device_id = d1_dvc.d1_device_id
AND d1_dvc_identifier.dvc_id_type_flg = 'D1BN'
JOIN cisadm.d1_sp_identifier
  ON d1_sp_identifier.d1_sp_id = d1_install_evt.d1_sp_id
AND d1_sp_identifier.sp_id_type_flg IN ('D1EP')
LEFT JOIN cisadm.ci_prem_geo
  ON ci_prem_geo.prem_id = d1_sp_identifier.id_value
AND ci_prem_geo.geo_type_cd = 'LAT/LONG'
JOIN cisadm.d1_measr_comp
  ON d1_measr_comp.device_config_id = d1_dvc_cfg.device_config_id
AND d1_measr_comp.measr_comp_type_cd = 'E-V2HVHA-15'
JOIN cisadm.d1_msrmt
  ON d1_msrmt.measr_comp_id = d1_measr_comp.measr_comp_id
AND d1_msrmt.msrmt_cond_flg = '501000'
WHERE d1_msrmt.msrmt_dttm >= :hour_utc
  AND d1_msrmt.msrmt_dttm < :hour_utc + INTERVAL '1' HOUR
"""

def connect_c2m() -> cx_Oracle.Connection:
    conn = get_c2m_connection()
    with conn.cursor() as cur:
        # Ensure session uses UTC
        cur.execute("ALTER SESSION SET TIME_ZONE = 'UTC'")
    return conn

def fetch_hour_df(conn: cx_Oracle.Connection, hour_utc: datetime) -> pd.DataFrame:
    """
    hour_utc should be timezone-aware UTC datetime.
    Returns DataFrame columns:
      BADGE, DEVICE_TYPE_CD, BADGE_LAT, BADGE_LONG, NOMINAL, MSRMTDTTM, ACTUAL, PUVALUE
    """
    if hour_utc.tzinfo is None:
        raise ValueError("hour_utc must be timezone-aware (UTC).")
    with conn.cursor() as cur:
        cur.arraysize = ARRAYSIZE
        # The session is UTC, so strip tzinfo from the Python datetime object
        cur.execute(SQL_HOURLY, hour_utc=hour_utc.replace(tzinfo=None))
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
    df = pd.DataFrame.from_records(rows, columns=cols)
    return df