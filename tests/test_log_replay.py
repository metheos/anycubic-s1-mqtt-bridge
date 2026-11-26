import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ["ANYCUBIC_S1_IP"] = "192.0.2.10"
os.environ["OBICO_ENABLED"] = "false"

import asm  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "fw_2727_print_run.jsonl"
TOPIC_BASE = "anycubic/anycubicCloud/v1/printer/public/20025/0123456789abcdef0123456789abcdef"
FILENAME = "1126-1027-Silver Silk PLA(03)_PLA_0.3_1h22m51s.gcode.3mf"


def collapse(values):
    result = []
    for value in values:
        if not result or result[-1] != value:
            result.append(value)
    return result


class BridgeHarness:
    """Drives the real message handler with a mocked Home Assistant client."""

    def __init__(self):
        bridge = object.__new__(asm.AnycubicMqttBridge)
        bridge.ha_client = Mock()
        bridge.anycubic_client = Mock()
        bridge.stream_url = None
        bridge.snapshot_thread = None
        bridge.snapshot_interval = 30
        bridge.current_print_state = "unknown"
        bridge.printer_state = "unknown"
        bridge.printer_cn = "TEST-CN"
        bridge.printer_mode_id = "20025"
        bridge.printer_device_id = "0123456789abcdef0123456789abcdef"
        bridge.printer_model = "Kobra S1"
        bridge.discovered_color_box_ids = set()
        bridge.discovered_color_box_firmware = {}
        bridge.latest_color_box_data = {}
        self.bridge = bridge
        self.printer_states = []
        self.print_jobs = []
        bridge.ha_client.publish.side_effect = self._record

    def _record(self, topic, payload=None, *args, **kwargs):
        prefix = self.bridge.get_topic_prefix()
        if topic == f"homeassistant/sensor/{prefix}/state":
            self.printer_states.append(json.loads(payload)["state"])
        elif topic == f"homeassistant/sensor/{prefix}/print_job":
            self.print_jobs.append(json.loads(payload))

    def send(self, topic_type, payload):
        msg = SimpleNamespace(
            topic=f"{TOPIC_BASE}/{topic_type}/report",
            payload=json.dumps(payload).encode("utf-8"),
        )
        with patch.object(asm, "Thread"):
            self.bridge.on_anycubic_message(None, None, msg)

    def replay(self, records):
        for record in records:
            self.send(record["topic_type"], record["payload"])
        return self


def load_fixture():
    with FIXTURE.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle]


def info(state, project=None, last_project=None):
    return {
        "type": "info",
        "action": "report",
        "data": {"state": state, "project": project, "last_project": last_project},
    }


def project(state, progress=50, **extra):
    data = {
        "filename": FILENAME,
        "state": state,
        "progress": progress,
        "curr_layer": 90,
        "total_layers": 193,
        "print_time": 40,
        "remain_time": 30,
        "supplies_usage": 8000,
    }
    data.update(extra)
    return data


SLICE_PARAM_DONE = {
    "type": "print",
    "action": "getSliceParam",
    "state": "done",
    "code": 200,
    "msg": "",
    "data": {"taskid": "-1", "slice_param": {"remain_time": 70}},
}


class RecordedFirmware2727PrintRunTests(unittest.TestCase):
    """Replays the 2.7.2.7 print run that showed state flapping (issue #12)."""

    @classmethod
    def setUpClass(cls):
        cls.harness = BridgeHarness().replay(load_fixture())

    def test_printer_state_follows_print_lifecycle_without_flapping(self):
        self.assertEqual(
            collapse(self.harness.printer_states),
            ["leveling", "preheating", "busy", "free"],
        )

    def test_print_job_state_follows_print_lifecycle_without_flapping(self):
        self.assertEqual(
            collapse(job["state"] for job in self.harness.print_jobs),
            ["leveling", "preheating", "printing", "finished"],
        )

    def test_job_details_are_never_blanked_by_slice_parameter_replies(self):
        for job in self.harness.print_jobs:
            self.assertEqual(job["filename"], FILENAME)
            self.assertEqual(job["total_layers"], 193)
            self.assertIsNotNone(job["remaining_time"])

    def test_progress_never_regresses_while_printing(self):
        progress = [
            job["progress"]
            for job in self.harness.print_jobs
            if job["state"] == "printing"
        ]
        self.assertEqual(progress, sorted(progress))
        self.assertEqual(progress[-1], 100)

    def test_final_state_reports_completed_print(self):
        bridge = self.harness.bridge
        self.assertEqual(bridge.printer_state, "free")
        self.assertEqual(bridge.current_print_state, "finished")
        final_job = self.harness.print_jobs[-1]
        self.assertEqual(final_job["state"], "finished")
        self.assertEqual(final_job["progress"], 100)
        self.assertEqual(final_job["remaining_time"], 0)
        self.assertIsNone(final_job["estimated_completion"])


class SimulatedPrintLifecycleTests(unittest.TestCase):
    def test_slice_parameter_reply_during_leveling_keeps_leveling(self):
        harness = BridgeHarness()
        harness.send("info", info("busy", project("auto_leveling", progress=0)))
        harness.send("print", SLICE_PARAM_DONE)

        self.assertEqual(harness.bridge.printer_state, "leveling")
        self.assertEqual(harness.print_jobs[-1]["state"], "leveling")
        self.assertEqual(harness.print_jobs[-1]["filename"], FILENAME)

    def test_idle_info_after_missed_finish_report_ends_print(self):
        harness = BridgeHarness()
        harness.send("info", info("busy", project("printing")))
        harness.send(
            "info",
            info("free", None, last_project=project("finished", progress=100)),
        )

        self.assertEqual(harness.bridge.printer_state, "free")
        self.assertEqual(harness.bridge.current_print_state, "finished")
        self.assertEqual(harness.print_jobs[-1]["state"], "finished")
        self.assertEqual(harness.print_jobs[-1]["progress"], 100)

    def test_transient_free_info_with_active_project_stays_busy(self):
        harness = BridgeHarness()
        harness.send("info", info("busy", project("printing")))
        harness.send("info", info("free", project("printing")))

        self.assertEqual(harness.bridge.printer_state, "busy")
        self.assertEqual(harness.bridge.current_print_state, "printing")

    def test_bridge_started_after_print_reports_idle(self):
        harness = BridgeHarness()
        harness.send(
            "info",
            info("free", None, last_project=project("finished", progress=100)),
        )

        self.assertEqual(harness.printer_states[-1], "free")

    def test_pause_resume_and_cancel_lifecycle(self):
        harness = BridgeHarness()

        def print_report(action, state, **extra):
            return {
                "type": "print",
                "action": action,
                "state": state,
                "code": 200,
                "msg": "",
                "data": project(state, **extra),
            }

        harness.send("print", print_report("start", "printing"))
        harness.send("print", print_report("pause", "paused", progress=55))
        harness.send("print", SLICE_PARAM_DONE)
        harness.send("info", info("busy", project("paused", progress=55)))
        self.assertEqual(harness.bridge.printer_state, "paused")
        self.assertEqual(harness.print_jobs[-1]["state"], "paused")

        harness.send("print", print_report("resume", "resumed", progress=56))
        self.assertEqual(harness.bridge.printer_state, "busy")

        harness.send("print", print_report("stop", "stoped", progress=56))
        harness.send("info", info("free", None))
        self.assertEqual(collapse(harness.printer_states), ["busy", "paused", "busy", "free"])
        self.assertEqual(harness.print_jobs[-1]["state"], "free")


class TemperatureDiscoveryTests(unittest.TestCase):
    def test_target_temperature_sensors_and_celsius_controls(self):
        harness = BridgeHarness()
        report = info("busy", project("printing"))
        report["data"]["temp"] = {
            "curr_hotbed_temp": 60,
            "curr_nozzle_temp": 210,
            "target_hotbed_temp": 60,
            "target_nozzle_temp": 210,
        }
        harness.send("info", report)

        configs = {}
        for call in harness.bridge.ha_client.publish.call_args_list:
            topic, payload = call.args[0], call.args[1] if len(call.args) > 1 else None
            if topic.endswith("/config") and payload:
                configs[topic] = json.loads(payload)
        prefix = harness.bridge.get_topic_prefix()

        for key in ("target_hotbed_temp", "target_nozzle_temp"):
            sensor = configs[f"homeassistant/sensor/{prefix}_{key}/config"]
            self.assertEqual(sensor["unit_of_measurement"], "°C")
            control = configs[f"homeassistant/number/{prefix}_{key}_control/config"]
            self.assertEqual(control["unit_of_measurement"], "°C")


if __name__ == "__main__":
    unittest.main()
