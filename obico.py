import base64
from urllib.parse import urlsplit, urlunsplit

import requests


DEFAULT_OBICO_API_URL = "http://localhost:3333/detect/"
DEFAULT_OBICO_API_HOST = "localhost"
SUPERVISOR_API_URL = "http://supervisor"
def build_api_url(api_host):
    value = (api_host or "").strip()
    if not value:
        value = DEFAULT_OBICO_API_HOST

    if "://" in value:
        return value

    parsed_host = urlsplit(f"http://{value}")
    if not parsed_host.netloc:
        raise ValueError("Obico API hostname is invalid")

    netloc = parsed_host.netloc
    if parsed_host.port is None:
        netloc = f"{netloc}:3333"
    path = parsed_host.path.rstrip("/")
    if not path or path == "/":
        path = "/detect/"
    elif path != "/detect":
        raise ValueError("Use only the Obico API hostname")
    else:
        path = "/detect/"

    return urlunsplit(("http", netloc, path, "", ""))


def discover_obico_api_url(supervisor_token, fallback_host, timeout=3):
    fallback_url = build_api_url(fallback_host)
    if not supervisor_token:
        return fallback_url, "SUPERVISOR_TOKEN is unavailable; using fallback hostname"

    headers = {"Authorization": f"Bearer {supervisor_token}"}
    try:
        response = requests.get(
            f"{SUPERVISOR_API_URL}/addons", headers=headers, timeout=timeout
        )
        response.raise_for_status()
        addons = response.json().get("data", {}).get("addons", [])
        obico_addon = next(
            (
                addon
                for addon in addons
                if addon.get("name", "").strip().casefold() == "obico ml"
                or addon.get("slug", "").endswith("_obico_ml")
            ),
            None,
        )
        if obico_addon is None:
            return (
                fallback_url,
                f"Obico ML add-on not found among {len(addons)} installed add-ons; using fallback hostname",
            )

        slug = obico_addon.get("slug")
        if not slug:
            return fallback_url, "Obico ML add-on has no slug; using fallback hostname"

        info_response = requests.get(
            f"{SUPERVISOR_API_URL}/addons/{slug}/info",
            headers=headers,
            timeout=timeout,
        )
        info_response.raise_for_status()
        hostname = info_response.json().get("data", {}).get("hostname")
        if not hostname:
            return fallback_url, f"Obico ML add-on '{slug}' has no hostname; using fallback hostname"
        return (
            build_api_url(hostname),
            f"found Obico ML add-on '{slug}' with hostname '{hostname}'",
        )
    except requests.HTTPError as err:
        status_code = err.response.status_code if err.response is not None else "unknown"
        return fallback_url, f"Supervisor API returned HTTP {status_code}; using fallback hostname"
    except requests.RequestException as err:
        return fallback_url, f"Supervisor API request failed ({type(err).__name__}); using fallback hostname"
    except (ValueError, TypeError, AttributeError):
        return fallback_url, "Supervisor API returned invalid add-on data; using fallback hostname"


def printer_is_busy(printer_state):
    return isinstance(printer_state, str) and printer_state.strip().lower() == "busy"


def resolve_printer_state(reported_state, print_job_state, previous_state):
    reported = str(reported_state or "").strip().lower()
    print_job = str(print_job_state or "").strip().lower()
    previous = str(previous_state or "").strip().lower()
    inactive_states = {"", "unknown", "unavailable", "free", "idle", "standby"}

    if print_job in {"leveling", "preheating"} and reported in inactive_states | {"busy"}:
        return print_job
    if print_job == "printing" and reported in inactive_states:
        return "busy"
    if print_job == "paused" and reported in inactive_states:
        return "paused"
    if reported in {"", "unknown", "unavailable"}:
        return previous if previous else "unknown"
    return reported


def resolve_print_job_state(action, reported_state, previous_state):
    action = str(action or "").strip().lower()
    reported = str(reported_state or "").strip().lower()
    previous = str(previous_state or "").strip().lower()

    if action == "getsliceparam":
        return previous or "unknown"
    if action == "start" and reported in {"auto_leveling", "autoleveling", "leveling"}:
        return "leveling"
    if action == "start" and reported == "preheating":
        return "preheating"
    if action in {"start", "resume"} and reported in {"printing", "busy"}:
        return "printing"
    if action == "pause":
        return "paused"
    if action in {"stop", "cancel", "finish", "complete"}:
        return "free"
    return previous or "unknown"


def merge_print_data(previous_data, incoming_data, print_state, reset=False):
    merged = {} if reset else dict(previous_data or {})
    incoming = incoming_data or {}
    incoming_filename = incoming.get("filename")
    if (
        incoming_filename
        and merged.get("filename")
        and incoming_filename != merged["filename"]
    ):
        merged = {}

    active_print_states = {"printing", "busy", "paused"}
    progress_fields = {
        "progress",
        "curr_layer",
        "total_layers",
        "print_time",
        "remain_time",
        "supplies_usage",
    }
    for key, value in incoming.items():
        if value is None or (key == "filename" and not value):
            continue
        if (
            print_state in active_print_states
            and key in progress_fields
            and key in merged
        ):
            try:
                if float(value) < float(merged[key]):
                    continue
            except (TypeError, ValueError):
                pass
        merged[key] = value
    return merged


def build_print_job_state(print_data, print_state):
    return {
        "filename": print_data.get("filename", ""),
        "progress": print_data.get("progress"),
        "current_layer": print_data.get("curr_layer"),
        "total_layers": print_data.get("total_layers"),
        "print_time": print_data.get("print_time"),
        "remaining_time": print_data.get("remain_time"),
        "material_usage": print_data.get("supplies_usage"),
        "state": print_state,
    }


def should_press_pause_button(error_detected, printer_state, pause_already_requested):
    return (
        error_detected
        and printer_is_busy(printer_state)
        and not pause_already_requested
    )


def check_api_health(api_url, timeout=3):
    try:
        parsed_url = urlsplit(api_url)
        if parsed_url.scheme not in ("http", "https") or not parsed_url.netloc:
            return False
        health_url = urlunsplit(
            (parsed_url.scheme, parsed_url.netloc, "/hc/", "", "")
        )
        response = requests.get(health_url, timeout=timeout)
        return response.status_code == 200 and response.text.strip().lower() == "ok"
    except (requests.RequestException, ValueError):
        return False


def detect_image(image_data, api_url, threshold, timeout=30):
    if not api_url:
        raise ValueError("Obico API URL is required")
    if not 0 <= threshold <= 1:
        raise ValueError("Obico threshold must be between 0 and 1")

    response = requests.post(
        api_url,
        json={
            "img": base64.b64encode(image_data).decode("ascii"),
            "threshold": threshold,
        },
        timeout=timeout,
    )
    response.raise_for_status()

    detection_data = response.json()
    detections = detection_data.get("detections", [])
    confidence_values = [float(detection[1]) for detection in detections]
    average_confidence = (
        round(sum(confidence_values) / len(confidence_values) * 100, 2)
        if confidence_values
        else 0
    )

    image_with_errors = detection_data.get("image_with_detections")
    if image_with_errors:
        image_with_errors = base64.b64decode(image_with_errors, validate=True)

    return {
        "error_detected": bool(detections),
        "detection_count": len(detections),
        "avg_confidence": average_confidence,
        "image_with_errors": image_with_errors,
    }


def detect_image_with_fallback(image_data, api_url, fallback_api_url, threshold):
    try:
        return detect_image(image_data, api_url, threshold), api_url, False
    except requests.ConnectionError:
        if fallback_api_url == api_url:
            raise
        result = detect_image(image_data, fallback_api_url, threshold)
        return result, fallback_api_url, True