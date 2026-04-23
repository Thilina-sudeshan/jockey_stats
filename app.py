from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Tuple

import pymysql
from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(
    title="Jockey stats API",
    version="4.0.0",
    description="Generate and persist pre-meeting jockey metrics using a flat tblruns table.",
)

app.add_middleware(SessionMiddleware, secret_key=os.getenv("SESSION_SECRET", "change-this-secret"))
templates = Jinja2Templates(directory="templates")

PRICE_BANDS: List[Tuple[str, float, float]] = [
    ("1.01-1.99", 1.01, 1.99),
    ("2.00-4.50", 2.00, 4.50),
    ("4.60-10.00", 4.60, 10.00),
    ("10.50-25.00", 10.50, 25.00),
    ("26.00+", 26.00, float("inf")),
]

REQUIRED_TBLRUNS_COLUMNS = {
    "meeting_id",
    "race_date",
    "race_course",
    "race_courseid",
    "race_no",
    "race_dist",
    "jockey_id",
    "jockey",
    "runner",
    "finish_pos",
    "sp_price",
}


class ConfigError(RuntimeError):
    pass


def _read_json_config() -> Dict[str, Any]:
    config_path = BASE_DIR / "config.json"
    if not config_path.exists():
        return {}
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid config.json: {exc}") from exc


CONFIG_JSON = _read_json_config()


def get_setting(name: str, default: Any = None) -> Any:
    env_name = name.upper()
    value = os.getenv(env_name)
    if value not in (None, ""):
        return value
    return CONFIG_JSON.get(name.lower(), default)


def get_db_config() -> Dict[str, Any]:
    host = get_setting("db_host")
    port = int(get_setting("db_port", 3306))
    user = get_setting("db_user")
    password = get_setting("db_password")
    database = get_setting("db_name")

    missing = [
        key for key, value in {
            "db_host": host,
            "db_user": user,
            "db_password": password,
            "db_name": database,
        }.items() if value in (None, "")
    ]
    if missing:
        raise ConfigError(f"Missing database settings: {', '.join(missing)}")

    return {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "database": database,
    }


@contextmanager
def db_connection() -> Generator[pymysql.connections.Connection, None, None]:
    config = get_db_config()
    conn = pymysql.connect(
        host=config["host"],
        port=config["port"],
        user=config["user"],
        password=config["password"],
        database=config["database"],
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )
    try:
        yield conn
    finally:
        conn.close()


def require_login_page(request: Request):
    if not request.session.get("user"):
        return RedirectResponse("/login", status_code=302)
    return None


def require_login_api(request: Request) -> None:
    if not request.session.get("user"):
        raise HTTPException(status_code=401, detail="Not authenticated")


def parse_finish_pos(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_sp(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def calc_wps(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    starts = len(rows)
    wins = sum(1 for row in rows if parse_finish_pos(row.get("finish_pos")) == 1)
    places = sum(1 for row in rows if (parse_finish_pos(row.get("finish_pos")) or 999) <= 3)
    return {
        "wins": wins,
        "win_pct": round((wins / starts) * 100, 1) if starts else 0.0,
        "places": places,
        "place_pct": round((places / starts) * 100, 1) if starts else 0.0,
        "starts": starts,
    }


def get_price_band(sp_price: Optional[float]) -> Optional[str]:
    if sp_price is None:
        return None
    for label, lower, upper in PRICE_BANDS:
        if lower <= sp_price <= upper:
            return label
    return None


def calc_meeting_averages(jockey_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_meeting: Dict[Any, List[Dict[str, Any]]] = {}
    for row in jockey_rows:
        by_meeting.setdefault(row["meeting_id"], []).append(row)

    if not by_meeting:
        return {"avg_rides_per_meeting": 0.0, "avg_wins_per_meeting": 0.0}

    meeting_count = len(by_meeting)
    total_rides = sum(len(rows) for rows in by_meeting.values())
    total_wins = sum(sum(1 for row in rows if parse_finish_pos(row["finish_pos"]) == 1) for rows in by_meeting.values())

    return {
        "avg_rides_per_meeting": round(total_rides / meeting_count, 2),
        "avg_wins_per_meeting": round(total_wins / meeting_count, 2),
    }


def calc_meeting_distribution(jockey_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_meeting: Dict[Any, List[Dict[str, Any]]] = {}
    for row in jockey_rows:
        by_meeting.setdefault(row["meeting_id"], []).append(row)

    buckets = {
        "0_winners": {"count": 0, "last_date": None},
        "1_winner": {"count": 0, "last_date": None},
        "2_winners": {"count": 0, "last_date": None},
        "3_winners": {"count": 0, "last_date": None},
        "4_winners": {"count": 0, "last_date": None},
        "5plus_winners": {"count": 0, "last_date": None},
    }

    for rows in by_meeting.values():
        wins = sum(1 for row in rows if parse_finish_pos(row["finish_pos"]) == 1)
        meeting_date = max(row["race_date"] for row in rows)

        if wins == 0:
            bucket = "0_winners"
        elif wins == 1:
            bucket = "1_winner"
        elif wins == 2:
            bucket = "2_winners"
        elif wins == 3:
            bucket = "3_winners"
        elif wins == 4:
            bucket = "4_winners"
        else:
            bucket = "5plus_winners"

        buckets[bucket]["count"] += 1
        current_last = buckets[bucket]["last_date"]
        if current_last is None or meeting_date > current_last:
            buckets[bucket]["last_date"] = meeting_date

    for value in buckets.values():
        if value["last_date"] is not None:
            value["last_date"] = value["last_date"].isoformat()

    return buckets


def calc_price_band_baselines(all_rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {label: [] for label, _, _ in PRICE_BANDS}
    for row in all_rows:
        band = get_price_band(parse_sp(row.get("sp_price")))
        if band:
            grouped[band].append(row)

    baselines: Dict[str, Dict[str, Any]] = {}
    for band, rows in grouped.items():
        wps = calc_wps(rows)
        baselines[band] = {
            **wps,
            "win_rate": (wps["wins"] / wps["starts"]) if wps["starts"] else 0.0,
        }
    return baselines


def calc_price_band_ivs(jockey_rows: List[Dict[str, Any]], baselines: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {label: [] for label, _, _ in PRICE_BANDS}
    for row in jockey_rows:
        band = get_price_band(parse_sp(row.get("sp_price")))
        if band:
            grouped[band].append(row)

    output: List[Dict[str, Any]] = []
    for band, _, _ in PRICE_BANDS:
        rows = grouped[band]
        jockey_wps = calc_wps(rows)
        jockey_win_rate = (jockey_wps["wins"] / jockey_wps["starts"]) if jockey_wps["starts"] else 0.0
        baseline = baselines[band]
        baseline_win_rate = baseline["win_rate"]
        iv = round(jockey_win_rate / baseline_win_rate, 3) if baseline_win_rate > 0 else 0.0

        output.append({
            "band": band,
            "jockey_wps": jockey_wps,
            "baseline_wps": {
                "wins": baseline["wins"],
                "win_pct": baseline["win_pct"],
                "places": baseline["places"],
                "place_pct": baseline["place_pct"],
                "starts": baseline["starts"],
            },
            "iv": iv,
        })

    return output


def validate_tblruns_schema() -> Dict[str, Any]:
    with db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SHOW COLUMNS FROM tblruns")
            columns = {row["Field"] for row in cursor.fetchall()}

    missing = sorted(REQUIRED_TBLRUNS_COLUMNS - columns)
    return {
        "ok": len(missing) == 0,
        "columns": sorted(columns),
        "missing_columns": missing,
    }


def fetch_target_meeting_rows(target_date: date, course_name: Optional[str], course_id: Optional[int]) -> Dict[str, Any]:
    with db_connection() as conn:
        with conn.cursor() as cursor:
            if course_id is not None:
                cursor.execute(
                    """
                    SELECT
                        meeting_id,
                        race_date,
                        race_course,
                        race_courseid,
                        race_no,
                        race_dist,
                        jockey_id,
                        jockey,
                        runner,
                        finish_pos,
                        sp_price
                    FROM tblruns
                    WHERE race_date = %s
                      AND race_courseid = %s
                    ORDER BY race_date ASC, race_no ASC, jockey_id ASC
                    """,
                    (target_date, course_id),
                )
            else:
                cursor.execute(
                    """
                    SELECT
                        meeting_id,
                        race_date,
                        race_course,
                        race_courseid,
                        race_no,
                        race_dist,
                        jockey_id,
                        jockey,
                        runner,
                        finish_pos,
                        sp_price
                    FROM tblruns
                    WHERE race_date = %s
                      AND TRIM(UPPER(race_course)) = TRIM(UPPER(%s))
                    ORDER BY race_date ASC, race_no ASC, jockey_id ASC
                    """,
                    (target_date, course_name),
                )

            exact_rows = cursor.fetchall()

            if exact_rows:
                return {
                    "match_type": "exact",
                    "rows": exact_rows,
                    "suggestion": None
                }

            # fallback, nearest available date for same course
            if course_id is not None:
                cursor.execute(
                    """
                    SELECT DISTINCT race_date, race_course, race_courseid
                    FROM tblruns
                    WHERE race_courseid = %s
                    ORDER BY ABS(DATEDIFF(race_date, %s)) ASC, race_date DESC
                    LIMIT 1
                    """,
                    (course_id, target_date),
                )
            else:
                cursor.execute(
                    """
                    SELECT DISTINCT race_date, race_course, race_courseid
                    FROM tblruns
                    WHERE TRIM(UPPER(race_course)) = TRIM(UPPER(%s))
                    ORDER BY ABS(DATEDIFF(race_date, %s)) ASC, race_date DESC
                    LIMIT 1
                    """,
                    (course_name, target_date),
                )

            nearest = cursor.fetchone()

            if not nearest:
                return {
                    "match_type": "none",
                    "rows": [],
                    "suggestion": None
                }

            cursor.execute(
                """
                SELECT
                    meeting_id,
                    race_date,
                    race_course,
                    race_courseid,
                    race_no,
                    race_dist,
                    jockey_id,
                    jockey,
                    runner,
                    finish_pos,
                    sp_price
                FROM tblruns
                WHERE race_date = %s
                  AND race_courseid = %s
                ORDER BY race_date ASC, race_no ASC, jockey_id ASC
                """,
                (nearest["race_date"], nearest["race_courseid"]),
            )
            fallback_rows = cursor.fetchall()

            return {
                "match_type": "nearest",
                "rows": fallback_rows,
                "suggestion": {
                    "requested_date": str(target_date),
                    "requested_course": course_name,
                    "used_date": str(nearest["race_date"]),
                    "used_course": nearest["race_course"],
                    "used_courseid": nearest["race_courseid"],
                },
            }

def fetch_history_rows(target_date: date, jockey_ids: List[Any]) -> List[Dict[str, Any]]:
    if not jockey_ids:
        return []

    placeholders = ",".join(["%s"] * len(jockey_ids))
    params: List[Any] = list(jockey_ids)
    params.extend([target_date, target_date - timedelta(days=365)])

    sql = f"""
        SELECT
            meeting_id,
            race_date,
            race_course,
            race_courseid,
            race_no,
            race_dist,
            jockey_id,
            jockey,
            runner,
            finish_pos,
            sp_price
        FROM tblruns
        WHERE jockey_id IN ({placeholders})
          AND race_date < %s
          AND race_date >= %s
        ORDER BY race_date ASC, race_no ASC, race_course ASC, meeting_id ASC, jockey_id ASC
    """

    with db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            rows = cursor.fetchall()

    return rows


def build_payload(target_rows, history_rows, target_date, requested_course_name, requested_course_id):
    if not target_rows:
        raise HTTPException(status_code=404, detail="No target meeting rows found for the selected date and course.")

    target_meeting_id = target_rows[0].get("meeting_id")
    target_course_name = requested_course_name or target_rows[0].get("race_course")
    target_course_id = requested_course_id or target_rows[0].get("race_courseid")

    by_jockey: Dict[int, List[Dict[str, Any]]] = {}
    cleaned_history_rows: List[Dict[str, Any]] = []

    for row in history_rows:
        cleaned = {
            "meeting_id": row["meeting_id"],
            "race_date": row["race_date"],
            "race_course": row["race_course"],
            "race_courseid": row["race_courseid"],
            "race_no": row["race_no"],
            "race_dist": row["race_dist"],
            "jockey_id": row["jockey_id"],
            "jockey": row["jockey"],
            "runner": row["runner"],
            "finish_pos": parse_finish_pos(row.get("finish_pos")),
            "sp_price": parse_sp(row.get("sp_price")),
        }
        cleaned_history_rows.append(cleaned)
        by_jockey.setdefault(cleaned["jockey_id"], []).append(cleaned)

    baselines = calc_price_band_baselines(cleaned_history_rows)
    jockeys: List[Dict[str, Any]] = []

    target_jockeys = sorted({row["jockey_id"] for row in target_rows if row.get("jockey_id") is not None})

    for jockey_id in target_jockeys:
        jockey_rows = by_jockey.get(jockey_id, [])
        target_name_match = [r for r in target_rows if r.get("jockey_id") == jockey_id]
        if target_name_match:
            jockey_name = target_name_match[0].get("jockey")
        elif jockey_rows:
            jockey_name = sorted(jockey_rows, key=lambda r: (r["race_date"], r["race_no"], r["meeting_id"]))[-1].get("jockey")
        else:
            jockey_name = f"Jockey {jockey_id}"

        if jockey_rows:
            jockey_rows = sorted(jockey_rows, key=lambda r: (r["race_date"], r["race_no"], r["race_course"], r["meeting_id"]))
            for seq_no, ride in enumerate(jockey_rows, start=1):
                ride["seq_no"] = seq_no

            last_meeting_date = max(ride["race_date"] for ride in jockey_rows)
            last_meeting_rows = [ride for ride in jockey_rows if ride["race_date"] == last_meeting_date]
            last_meeting_rows.sort(key=lambda r: (r["race_no"], r["race_course"], r["meeting_id"]))
            last_meeting = {
                "date": last_meeting_date.isoformat(),
                "course": last_meeting_rows[0]["race_course"],
                "days_since": (target_date - last_meeting_date).days,
                "wps": calc_wps(last_meeting_rows),
            }

            winning_rides = [ride for ride in jockey_rows if ride["finish_pos"] == 1]
            if winning_rides:
                last_win_ride = winning_rides[-1]
                later_rides = [ride for ride in jockey_rows if ride["seq_no"] > last_win_ride["seq_no"]]
                later_meetings = {ride["meeting_id"] for ride in later_rides if ride["meeting_id"] != last_win_ride["meeting_id"]}
                last_win = {
                    "date": last_win_ride["race_date"].isoformat(),
                    "course": last_win_ride["race_course"],
                    "days_since": (target_date - last_win_ride["race_date"]).days,
                    "rides_since": len(later_rides),
                    "meetings_since": len(later_meetings),
                }
            else:
                last_win = {"date": None, "course": None, "days_since": None, "rides_since": None, "meetings_since": None}

            at_course_rows = [
                ride for ride in jockey_rows
                if (target_course_id is not None and ride["race_courseid"] == target_course_id)
                or (target_course_name is not None and ride["race_course"] == target_course_name)
            ]
            if at_course_rows:
                last_course_date = max(ride["race_date"] for ride in at_course_rows)
                last_course_meeting_rows = [ride for ride in at_course_rows if ride["race_date"] == last_course_date]
                last_course_meeting_rows.sort(key=lambda r: (r["race_no"], r["meeting_id"]))
                last_time_at_course = {
                    "course": last_course_meeting_rows[0]["race_course"],
                    "course_id": last_course_meeting_rows[0]["race_courseid"],
                    "date": last_course_date.isoformat(),
                    "days_since": (target_date - last_course_date).days,
                    "wps": calc_wps(last_course_meeting_rows),
                }
            else:
                last_time_at_course = {
                    "course": target_course_name,
                    "course_id": target_course_id,
                    "date": None,
                    "days_since": None,
                    "wps": calc_wps([]),
                }

            rolling_stats = {
                "last_365_days": calc_wps(jockey_rows),
                "last_30_days": calc_wps([ride for ride in jockey_rows if ride["race_date"] >= target_date - timedelta(days=30)]),
                "last_7_days": calc_wps([ride for ride in jockey_rows if ride["race_date"] >= target_date - timedelta(days=7)]),
            }

            meeting_averages = calc_meeting_averages(jockey_rows)
            meeting_distribution = calc_meeting_distribution(jockey_rows)
            price_band_ivs = calc_price_band_ivs(jockey_rows, baselines)
        else:
            last_meeting = {"date": None, "course": None, "days_since": None, "wps": calc_wps([])}
            last_win = {"date": None, "course": None, "days_since": None, "rides_since": None, "meetings_since": None}
            last_time_at_course = {"course": target_course_name, "course_id": target_course_id, "date": None, "days_since": None, "wps": calc_wps([])}
            rolling_stats = {"last_365_days": calc_wps([]), "last_30_days": calc_wps([]), "last_7_days": calc_wps([])}
            meeting_averages = calc_meeting_averages([])
            meeting_distribution = calc_meeting_distribution([])
            price_band_ivs = calc_price_band_ivs([], baselines)

        jockeys.append({
            "jockey_id": jockey_id,
            "jockey_name": jockey_name,
            "last_meeting": last_meeting,
            "last_win": last_win,
            "last_time_at_course": last_time_at_course,
            "rolling_stats": rolling_stats,
            "meeting_averages": meeting_averages,
            "meeting_distribution": meeting_distribution,
            "price_band_ivs": price_band_ivs,
        })

    jockeys.sort(key=lambda j: (str(j["jockey_name"]), j["jockey_id"]))
    return {
        "meeting": {
            "meeting_id": target_meeting_id,
            "course": target_course_name,
            "course_id": target_course_id,
            "date": target_date.isoformat(),
        },
        "jockeys": jockeys,
    }


def persist_payload(payload):
    persist_enabled = str(get_setting("persist_enabled", "true")).lower() == "true"
    if not persist_enabled:
        return

    stats_table = get_setting("persist_stats_table", "meeting_jockey_stats")
    meeting_id = payload["meeting"].get("meeting_id")
    if meeting_id is None:
        raise ConfigError("Cannot persist payload because meeting_id is missing.")

    with db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {stats_table} (
                    meeting_id BIGINT NOT NULL PRIMARY KEY,
                    generated_at_utc DATETIME NOT NULL,
                    payload_json JSON NOT NULL
                )
                """
            )
            cursor.execute(
                f"""
                INSERT INTO {stats_table} (meeting_id, generated_at_utc, payload_json)
                VALUES (%s, UTC_TIMESTAMP(), %s)
                ON DUPLICATE KEY UPDATE
                    generated_at_utc = VALUES(generated_at_utc),
                    payload_json = VALUES(payload_json)
                """,
                (meeting_id, json.dumps(payload)),
            )


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if request.session.get("user"):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login", response_class=HTMLResponse)
def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    if username == "admin" and password == "admin123":
        request.session["user"] = username
        return RedirectResponse("/", status_code=302)

    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": "Invalid username or password"},
        status_code=401,
    )


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    redirect = require_login_page(request)
    if redirect:
        return redirect

    return templates.TemplateResponse("index.html", {"request": request, "user": request.session.get("user")})


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/db-test")
def db_test(request: Request):
    require_login_api(request)
    try:
        schema_check = validate_tblruns_schema()
        with db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT DATABASE() AS db_name, NOW() AS server_time")
                row = cursor.fetchone()
                cursor.execute("SHOW TABLES")
                tables = [list(t.values())[0] for t in cursor.fetchall()]

        return {"ok": True, "database": row["db_name"], "server_time": str(row["server_time"]), "tables": tables, "tblruns_schema": schema_check}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/jockey-stats")
def get_jockey_stats(
    request: Request,
    date_value: date = Query(..., alias="date"),
    course_name: Optional[str] = Query(None, alias="courseName"),
    course_id: Optional[int] = Query(None, alias="courseId"),
    persist: bool = Query(False, alias="persist"),
):
    require_login_api(request)

    if (course_name is None and course_id is None) or (course_name is not None and course_id is not None):
        raise HTTPException(
            status_code=400,
            detail="Provide exactly one of courseName or courseId, along with date.",
        )

    try:
        schema_check = validate_tblruns_schema()
        if not schema_check["ok"]:
            raise HTTPException(
                status_code=500,
                detail=f"tblruns is missing required columns: {', '.join(schema_check['missing_columns'])}"
            )

        target_result = fetch_target_meeting_rows(
            target_date=date_value,
            course_name=course_name,
            course_id=course_id,
        )

        target_rows = target_result["rows"]

        if not target_rows:
            raise HTTPException(
                status_code=404,
                detail="No rows found for that course, and no nearby relevant rows were found."
            )

        jockey_ids = sorted({
            row["jockey_id"] for row in target_rows
            if row.get("jockey_id") is not None
        })

        history_rows = fetch_history_rows(date_value, jockey_ids)

        payload = build_payload(
            target_rows=target_rows,
            history_rows=history_rows,
            target_date=date_value,
            requested_course_name=course_name,
            requested_course_id=course_id,
        )

        payload["selection_match"] = target_result["match_type"]
        payload["selection_info"] = target_result["suggestion"]

        if persist:
            persist_payload(payload)

        return JSONResponse(content=payload)

    except HTTPException as exc:
        raise exc
    except ConfigError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except pymysql.MySQLError as exc:
        raise HTTPException(status_code=500, detail=f"Database error: {exc}") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {exc}") from exc


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
