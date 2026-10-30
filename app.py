"""Small web service for the Ansible community open polls page."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import yaml
from flask import Flask, Response, jsonify, render_template_string, send_file


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
logger = logging.getLogger("openpolls")
app = Flask(__name__)

FORUM_BASE_URL = "https://forum.ansible.com"
DEFAULT_API_CALL = "/admin/plugins/explorer/queries/2/run"
CONFIG_PATH = Path(os.getenv("CONFIG_PATH", "/etc/open-polls/polls.yml"))
try:
    CACHE_SECONDS = max(30, int(os.getenv("CACHE_SECONDS", "300")))
except ValueError:
    logger.warning("Invalid CACHE_SECONDS value; using the 300 second default")
    CACHE_SECONDS = 300
_cache_lock = threading.Lock()
_cached_rows: list[dict] | None = None
_cached_at = 0.0
_last_attempt_at = 0.0
_last_error: str | None = None


class PollServiceError(Exception):
    """Expected configuration or upstream errors that can be shown to clients."""


def configuration() -> dict[str, str]:
    """Read optional non-secret settings and the Forum API key from the environment."""
    values: dict[str, str] = {}
    if CONFIG_PATH.is_file():
        try:
            with CONFIG_PATH.open(encoding="utf-8") as stream:
                config = yaml.safe_load(stream) or {}
        except (OSError, yaml.YAMLError) as error:
            raise PollServiceError(f"Could not read config file {CONFIG_PATH}: {error}") from None
        if not isinstance(config, dict):
            raise PollServiceError(f"Config file {CONFIG_PATH} must contain a YAML mapping")
        values = config.get("default", config)
        if not isinstance(values, dict):
            raise PollServiceError("The 'default' config section must be a YAML mapping")

    settings = {
        "api_user": os.getenv(
            "FORUM_POLL_USERNAME",
            values.get("api_user", "gundalow"),
        ),
        "api_key": os.getenv("FORUM_POLL_API", "").strip(),
        "api_url": os.getenv(
            "FORUM_POLL_URL",
            values.get("api_url", FORUM_BASE_URL),
        ),
        "api_call": os.getenv(
            "FORUM_POLL_API_CALL",
            values.get("api_call", DEFAULT_API_CALL),
        ),
    }
    if not settings["api_key"]:
        raise PollServiceError("The FORUM_POLL_API secret is not set")
    for key in ("api_user", "api_url", "api_call"):
        if not settings[key]:
            raise PollServiceError(f"The {key} setting is empty")
    return settings


def _as_tags(value) -> list[str]:
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return []
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            value = parsed
        else:
            value = value.strip("{}").split(",")
    elif not isinstance(value, list):
        value = [value] if value else []
    return list(dict.fromkeys(str(tag).strip().strip('"') for tag in value if str(tag).strip()))


def normalize_result(payload: dict) -> list[dict]:
    """Convert the Data Explorer response into stable, page-ready poll records."""
    columns = payload.get("columns") or []
    rows = [dict(zip(columns, row)) for row in payload.get("rows", []) if isinstance(row, list)]
    relations = payload.get("relations") or {}
    topics = relations.get("topic", []) if isinstance(relations, dict) else []
    topics_by_id = {str(topic.get("id")): topic for topic in topics if topic.get("id") is not None}

    tag_columns = ("tag_name", "tag", "tags", "topic_tags", "tag_names")
    column_names = {str(column).strip().lower() for column in columns}
    if rows and not set(tag_columns).intersection(column_names):
        logger.warning(
            "Data Explorer result has no tag column; columns=%s, expected one of %s",
            columns,
            tag_columns,
        )

    grouped: dict[tuple, dict] = {}
    for row in rows:
        row = {str(name).strip().lower(): value for name, value in row.items()}
        topic_id = row.get("topic_id")
        topic = topics_by_id.get(str(topic_id), {})
        key_fields = (
            topic_id,
            row.get("num"),
            row.get("category_id"),
            row.get("cat_name"),
            row.get("close_at"),
            row.get("title"),
            row.get("fancy_title"),
            row.get("slug"),
        )
        key = tuple(str(value) if value is not None else "" for value in key_fields)
        record = grouped.setdefault(
            key,
            {
                "topic_id": topic_id,
                "post_number": row.get("num"),
                "category_id": row.get("category_id"),
                "category": row.get("cat_name"),
                "closes_at": row.get("close_at"),
                "title": row.get("title") or topic.get("title"),
                "display_title": row.get("fancy_title")
                or topic.get("fancy_title")
                or row.get("title"),
                "slug": row.get("slug") or topic.get("slug"),
                "tags": [],
                "topic_url": None,
                "category_url": None,
            },
        )
        tag_value = next(
            (row[name] for name in tag_columns if row.get(name)), topic.get("tags", [])
        )
        record["tags"] = list(dict.fromkeys(record["tags"] + _as_tags(tag_value)))

    result = list(grouped.values())
    if rows and not any(record["tags"] for record in result):
        logger.warning("Data Explorer returned poll rows but no tag values; columns=%s", columns)
    for record in result:
        if record["topic_id"] is not None and record["post_number"] is not None:
            record["topic_url"] = (
                f"https://forum.ansible.com/t/{record['topic_id']}/{record['post_number']}"
            )
        if record["category_id"] is not None:
            record["category_url"] = f"https://forum.ansible.com/c/{record['category_id']}"
    result.sort(key=lambda poll: (poll.get("closes_at") or "", poll.get("display_title") or ""))
    return result


def fetch_polls() -> list[dict]:
    settings = configuration()
    url = settings["api_url"].rstrip("/") + "/" + settings["api_call"].lstrip("/")
    body = urlencode({"params": "{}"}).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={
            "Api-Key": settings["api_key"],
            "Api-Username": settings["api_user"],
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": "ansible-open-polls/1.0",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        if error.code in (401, 403):
            raise PollServiceError(
                "The Forum rejected FORUM_POLL_API; check that the key is valid for "
                f"{settings['api_user']!r} and can access the Data Explorer query"
            ) from None
        if error.code == 429:
            retry_after = error.headers.get("Retry-After", "unknown")
            raise PollServiceError(
                f"The Forum rate limited the poll request (retry after {retry_after})"
            ) from None
        raise PollServiceError(
            f"The Forum returned HTTP {error.code} for the poll request"
        ) from None
    except (URLError, TimeoutError) as error:
        reason = getattr(error, "reason", error)
        raise PollServiceError(f"Could not reach the Forum poll API: {reason}") from None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise PollServiceError("The Forum poll API returned invalid JSON") from None
    if not isinstance(payload, dict):
        raise PollServiceError("The Forum poll API returned an unexpected response")
    if payload.get("errors"):
        raise PollServiceError("The Forum Data Explorer query returned an error")
    return normalize_result(payload)


def polls() -> list[dict]:
    global _cached_rows, _cached_at, _last_attempt_at, _last_error
    with _cache_lock:
        now = time.monotonic()
        if _cached_rows is not None and now - _cached_at < CACHE_SECONDS:
            return _cached_rows
        if _last_attempt_at and now - _last_attempt_at < CACHE_SECONDS:
            if _cached_rows is not None:
                return _cached_rows
            raise PollServiceError(_last_error or "Poll data is temporarily unavailable")

        _last_attempt_at = now
        try:
            _cached_rows = fetch_polls()
            _cached_at = now
            _last_error = None
        except PollServiceError as error:
            _last_error = str(error)
            logger.warning("Unable to refresh poll data: %s", error)
            if _cached_rows is None:
                raise PollServiceError(_last_error) from None
        except Exception:
            _last_error = "An unexpected error occurred while refreshing poll data"
            logger.exception(_last_error)
            if _cached_rows is None:
                raise PollServiceError(_last_error) from None
        return _cached_rows


PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Currently Open Ansible Community Votes</title>
  <style>
    :root { color-scheme: light; font-family: system-ui, sans-serif; color: #202428; }
    body { max-width: 1050px; margin: 2rem auto; padding: 0 1rem; }
    header { display: flex; align-items: center; gap: 1.5rem; margin-bottom: 2rem; }
    header img { width: min(230px, 35vw); height: auto; }
    h1 { font-size: clamp(1.6rem, 4vw, 2.3rem); }
    .table-wrap { overflow-x: auto; }
    table { border-collapse: collapse; width: 100%; }
    th, td { border-bottom: 1px solid #d9dee2; padding: .8rem .65rem; text-align: left; vertical-align: top; }
    th { background: #f2f4f5; }
    a { color: #0066b3; }
    #status { color: #555; }
    @media (max-width: 620px) { body { margin-top: 1rem; } header { align-items: flex-start; flex-direction: column; gap: .5rem; } }
  </style>
</head>
<body>
  <header><img src="/logo.png" alt="Ansible"><h1>Currently Open Ansible Community Votes</h1></header>
  <p id="status" role="status">Loading open polls…</p>
  <div class="table-wrap"><table>
    <thead><tr><th>Topic</th><th>Category</th><th>Closes</th><th>Tags</th></tr></thead>
    <tbody id="polls"></tbody>
  </table></div>
  <script>
    const cell = (row, value, href) => {
      const td = row.insertCell();
      if (href) { const a = document.createElement('a'); a.href = href; a.textContent = value || 'View'; td.append(a); }
      else td.textContent = value || '';
      return td;
    };
    fetch('/data.json').then(response => {
      if (!response.ok) throw new Error('Poll data is temporarily unavailable.');
      return response.json();
    }).then(data => {
      const body = document.getElementById('polls');
      for (const poll of data) {
        const row = body.insertRow();
        cell(row, poll.display_title, poll.topic_url);
        cell(row, poll.category, poll.category_url);
        cell(row, poll.closes_at ? poll.closes_at.slice(0, 10) : '');
        cell(row, (poll.tags || []).join(', '));
      }
      document.getElementById('status').textContent = data.length ? `${data.length} open poll(s)` : 'There are no open polls right now.';
    }).catch(error => { document.getElementById('status').textContent = error.message; });
  </script>
</body>
</html>"""


@app.get("/")
def index() -> str:
    return render_template_string(PAGE)


@app.get("/data.json")
def data_json() -> Response | tuple[Response, int]:
    try:
        records = polls()
    except PollServiceError as error:
        return jsonify({"error": str(error)}), 503
    except Exception:
        logger.exception("Unexpected error while serving poll data")
        return jsonify({"error": "Poll data is temporarily unavailable"}), 503
    response = jsonify(records)
    response.headers["Cache-Control"] = f"public, max-age={CACHE_SECONDS}"
    return response


@app.get("/logo.png")
def logo() -> Response:
    return send_file(
        Path(__file__).with_name("Ansible-Wordmark-Large-RGB-Black.png"), mimetype="image/png"
    )


@app.get("/healthz")
def healthz() -> Response:
    return Response("ok\n", mimetype="text/plain")


@app.get("/readyz")
def readyz() -> Response:
    try:
        polls()
    except PollServiceError:
        return Response("poll data is unavailable\n", status=503, mimetype="text/plain")
    except Exception:
        logger.exception("Unexpected error while checking poll data readiness")
        return Response("poll data is unavailable\n", status=503, mimetype="text/plain")
    if _cached_rows is not None:
        return Response("ready\n", mimetype="text/plain")
    return Response("poll data has not loaded\n", status=503, mimetype="text/plain")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
