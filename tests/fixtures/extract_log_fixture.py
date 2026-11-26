"""Build a sanitized JSONL replay fixture from a bridge DEBUG log.

Usage: python tests/fixtures/extract_log_fixture.py <bridge.log> <out.jsonl>

Each output line is {"topic_type": ..., "payload": {...}} in the order the
bridge received it. Info reports are logged without their envelope, so they
are rewrapped as {"type": "info", "action": "report", "data": ...}.
"""

import json
import re
import sys

LOG_PREFIX = re.compile(r"^[\w.-]+\s+\|\s?")
NEW_RECORD = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} - ")
INFO_RE = re.compile(r"Received printer info: (\{.*\})$")
PRINT_RE = re.compile(r"Print job report data: (\{.*\})$")
UNHANDLED_RE = re.compile(r"Unhandled message type: (status)/report$")

INFO_KEEP = ("state", "project", "last_project", "version", "print_speed_mode")
PRINT_DROP = ("source_info", "temp_dir", "temp_gcode", "origin3mf")


def _sanitize_info(data):
    return {key: data[key] for key in INFO_KEEP if key in data}


def _sanitize_print(message):
    message = dict(message)
    if isinstance(message.get("data"), dict):
        message["data"] = {
            key: value
            for key, value in message["data"].items()
            if key not in PRINT_DROP
        }
    return message


def extract(lines):
    lines = [LOG_PREFIX.sub("", line.rstrip("\n")) for line in lines]
    index = 0
    while index < len(lines):
        line = lines[index]
        match = INFO_RE.search(line)
        if match:
            yield {
                "topic_type": "info",
                "payload": {
                    "type": "info",
                    "action": "report",
                    "data": _sanitize_info(json.loads(match.group(1))),
                },
            }
        match = PRINT_RE.search(line)
        if match:
            yield {
                "topic_type": "print",
                "payload": _sanitize_print(json.loads(match.group(1))),
            }
        match = UNHANDLED_RE.search(line)
        if match and index + 1 < len(lines) and lines[index + 1].endswith(
            "Message data: {"
        ):
            body = ["{"]
            index += 2
            while index < len(lines) and not NEW_RECORD.match(lines[index]):
                body.append(lines[index])
                index += 1
            yield {"topic_type": match.group(1), "payload": json.loads("\n".join(body))}
            continue
        index += 1


def main(source, destination):
    with open(source, encoding="utf-8", errors="replace") as handle:
        records = list(extract(handle))
    with open(destination, "w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"Wrote {len(records)} messages to {destination}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
