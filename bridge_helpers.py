import requests


SUPERVISOR_API_URL = "http://supervisor"
PRINT_SPEED_MODE_NAMES = {
    1: "Silent",
    2: "Standard",
    3: "Sport",
}
MQTT_ERROR_MESSAGE_TRANSLATIONS = {
    "耗材不足": "Onvoldoende materialen",
    "设备忙": "Apparaat bezig",
    "温度过高": "Temperatuur te hoog",
    "用户发起": "Door gebruiker gestart",
}


def print_speed_mode_name(value):
    try:
        mode = int(value)
    except (TypeError, ValueError):
        return "Unknown"
    return PRINT_SPEED_MODE_NAMES.get(mode, f"Unknown ({mode})")


def get_firmware_version(data, keys):
    if not isinstance(data, dict):
        return None

    for key in keys:
        value = data.get(key)
        if value is None:
            continue
        version = str(value).strip()
        if version and version.casefold() not in {"unknown", "none", "null", "n/a"}:
            return version
    return None


def normalize_printer_device_name(name, model):
    display_name = str(name or "").strip()
    model_name = str(model or "").strip()
    if display_name.casefold().startswith("anycubic ") and model_name and model_name.casefold() != "unknown":
        return model_name
    return display_name or model_name or "Anycubic Printer"


def translate_mqtt_error_message(value):
    text = str(value or "").strip()
    return MQTT_ERROR_MESSAGE_TRANSLATIONS.get(text, text)


def classify_mqtt_error(message):
    message_type = str(message.get("type") or "").strip().lower()
    data = message.get("data")
    data = data if isinstance(data, dict) else {}
    project = data.get("project")
    project = project if isinstance(project, dict) else {}
    raw_state = message.get("state")
    if raw_state is None:
        raw_state = data.get("state", project.get("state"))
    state = str(raw_state or "").strip().lower()
    code = message.get("code", data.get("code"))
    text = str(
        message.get("msg")
        or message.get("message")
        or message.get("error")
        or data.get("msg")
        or data.get("message")
        or ""
    ).strip()
    normalized_text = text.lower()
    successful_message = (
        normalized_text in {"", "done", "success", "ok", "用户发起"}
        or normalized_text.endswith("/done")
    )
    job_failed = any(token in state for token in ("fail", "error", "abort", "cancel"))

    try:
        code_problem = code is not None and int(code) != 200
    except (TypeError, ValueError):
        code_problem = code is not None

    error_message = any(
        token in normalized_text
        for token in ("error", "fail", "abort", "cancel", "exception")
    )
    message_problem = message_type == "error" or (
        bool(text)
        and not successful_message
        and (message_type == "print" or error_message)
    )
    print_problem = job_failed or code_problem or message_problem

    return {
        "job_failed": job_failed,
        "print_problem": print_problem,
        "state": state,
        "code": code,
        "message": text,
    }


def get_color_box_id(box_data, fallback_index):
    try:
        return int(box_data.get("id", fallback_index))
    except (AttributeError, TypeError, ValueError):
        return int(fallback_index)


def normalize_box_slot_index(slot_index, box_id, slots_per_box=4):
    try:
        index = int(slot_index)
        box = int(box_id)
    except (TypeError, ValueError):
        return None

    if 0 <= index < slots_per_box:
        return index

    local_index = index - (slots_per_box * box)
    if 0 <= local_index < slots_per_box:
        return local_index
    return None


def global_filament_index(box_id, local_slot_index, slots_per_box=4):
    return int(box_id) * slots_per_box + int(local_slot_index)


def discover_mqtt_settings(supervisor_token, fallback_settings, timeout=3):
    fallback = dict(fallback_settings)
    if not supervisor_token:
        return fallback, "SUPERVISOR_TOKEN unavailable; using configured MQTT settings"

    try:
        response = requests.get(
            f"{SUPERVISOR_API_URL}/services/mqtt",
            headers={"Authorization": f"Bearer {supervisor_token}"},
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
        service = payload.get("data", payload)
        host = service.get("host")
        port = service.get("port")
        if not host or port is None:
            return fallback, "Supervisor MQTT service has no host/port; using configured settings"

        settings = {
            "broker": str(host),
            "port": int(port),
            "user": service.get("username") or fallback.get("user"),
            "password": service.get("password") or fallback.get("password"),
        }
        return settings, f"Supervisor MQTT service found at {settings['broker']}:{settings['port']}"
    except requests.HTTPError as err:
        status = err.response.status_code if err.response is not None else "unknown"
        return fallback, f"Supervisor MQTT lookup returned HTTP {status}; using configured settings"
    except requests.RequestException as err:
        return fallback, f"Supervisor MQTT lookup failed ({type(err).__name__}); using configured settings"
    except (ValueError, TypeError, AttributeError):
        return fallback, "Supervisor MQTT lookup returned invalid data; using configured settings"