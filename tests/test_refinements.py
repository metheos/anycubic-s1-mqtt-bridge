import unittest
from unittest.mock import Mock, patch

import requests

from bridge_helpers import (
    classify_mqtt_error,
    discover_mqtt_settings,
    get_firmware_version,
    global_filament_index,
    get_color_box_id,
    normalize_printer_device_name,
    normalize_box_slot_index,
    print_speed_mode_name,
    translate_mqtt_error_message,
)


class PrintSpeedModeTests(unittest.TestCase):
    def test_maps_anycubic_speed_enum(self):
        self.assertEqual(print_speed_mode_name(1), "Silent")
        self.assertEqual(print_speed_mode_name(2), "Standard")
        self.assertEqual(print_speed_mode_name(3), "Sport")

    def test_unknown_mode_stays_explicit(self):
        self.assertEqual(print_speed_mode_name(7), "Unknown (7)")


class DeviceNameTests(unittest.TestCase):
    def test_removes_redundant_manufacturer_prefix_when_model_is_known(self):
        self.assertEqual(
            normalize_printer_device_name("Anycubic Kobra S1", "Kobra S1"),
            "Kobra S1",
        )

    def test_preserves_custom_name(self):
        self.assertEqual(
            normalize_printer_device_name("Workshop Printer", "Kobra S1"),
            "Workshop Printer",
        )


class FirmwareVersionTests(unittest.TestCase):
    def test_uses_first_available_firmware_field(self):
        self.assertEqual(
            get_firmware_version(
                {"version": "Unknown", "fw_version": "1.2.3"},
                ("version", "fw_version"),
            ),
            "1.2.3",
        )

    def test_accepts_camel_case_firmware_fields(self):
        self.assertEqual(
            get_firmware_version({"firmwareVersion": "4.5.6"}, ("firmwareVersion",)),
            "4.5.6",
        )
        self.assertEqual(
            get_firmware_version({"fwVersion": "7.8.9"}, ("fwVersion",)),
            "7.8.9",
        )

    def test_ignores_missing_and_unknown_versions(self):
        self.assertIsNone(
            get_firmware_version({"version": "Unknown"}, ("version",))
        )
        self.assertIsNone(get_firmware_version(None, ("version",)))


class MqttErrorTests(unittest.TestCase):
    def test_translates_known_anycubic_messages_and_preserves_unknown_text(self):
        self.assertEqual(
            translate_mqtt_error_message("用户发起"), "Door gebruiker gestart"
        )
        self.assertEqual(translate_mqtt_error_message("设备忙"), "Apparaat bezig")
        self.assertEqual(
            translate_mqtt_error_message("Unexpected error"), "Unexpected error"
        )

    def test_user_initiated_print_message_is_not_a_problem(self):
        result = classify_mqtt_error(
            {"type": "print", "state": "printing", "code": 200, "msg": "用户发起"}
        )

        self.assertFalse(result["job_failed"])
        self.assertFalse(result["print_problem"])

    def test_normal_done_ack_is_not_an_error(self):
        result = classify_mqtt_error(
            {"type": "print", "state": "done", "code": 200, "msg": "getSliceParam/done"}
        )

        self.assertFalse(result["job_failed"])
        self.assertFalse(result["print_problem"])

    def test_failed_state_marks_both_error_entities(self):
        result = classify_mqtt_error(
            {"type": "print", "state": "failed", "code": 200, "msg": "print failed"}
        )

        self.assertTrue(result["job_failed"])
        self.assertTrue(result["print_problem"])

    def test_non_success_code_marks_print_problem_and_preserves_message(self):
        result = classify_mqtt_error(
            {"type": "print", "state": "done", "code": 11808, "msg": "bed error"}
        )

        self.assertFalse(result["job_failed"])
        self.assertTrue(result["print_problem"])
        self.assertEqual(result["code"], 11808)
        self.assertEqual(result["message"], "bed error")

    def test_error_type_without_code_is_reported(self):
        result = classify_mqtt_error(
            {"type": "error", "action": "setHotbedTemp", "message": "Target rejected"}
        )

        self.assertTrue(result["print_problem"])
        self.assertEqual(result["message"], "Target rejected")

    def test_failed_job_in_nested_info_project_is_detected(self):
        result = classify_mqtt_error(
            {"type": "info", "data": {"project": {"state": "cancelled"}}}
        )

        self.assertTrue(result["job_failed"])
        self.assertTrue(result["print_problem"])

    def test_non_success_print_message_is_reported_but_done_suffix_is_not(self):
        problem = classify_mqtt_error(
            {"type": "print", "code": 200, "msg": "temperature target rejected"}
        )
        acknowledgement = classify_mqtt_error(
            {"type": "print", "code": 200, "msg": "getSliceParam/done"}
        )

        self.assertTrue(problem["print_problem"])
        self.assertFalse(acknowledgement["print_problem"])

class ColorBoxIdTests(unittest.TestCase):
    def test_uses_firmware_box_id(self):
        self.assertEqual(get_color_box_id({"id": 1}, 0), 1)

    def test_falls_back_to_payload_position(self):
        self.assertEqual(get_color_box_id({}, 1), 1)
        self.assertEqual(get_color_box_id({"id": None}, 1), 1)

    def test_maps_four_local_slots_into_global_filament_numbers(self):
        self.assertEqual(global_filament_index(0, 0), 0)
        self.assertEqual(global_filament_index(0, 3), 3)
        self.assertEqual(global_filament_index(1, 0), 4)
        self.assertEqual(global_filament_index(1, 3), 7)

    def test_normalizes_box_two_global_slot_indexes(self):
        self.assertEqual(normalize_box_slot_index(4, 1), 0)
        self.assertEqual(normalize_box_slot_index(7, 1), 3)


class MqttServiceTests(unittest.TestCase):
    def setUp(self):
        self.fallback = {
            "broker": "manual-host",
            "port": 1883,
            "user": "manual-user",
            "password": "manual-pass",
        }

    @patch("bridge_helpers.requests.get")
    def test_uses_supervisor_service_and_credentials(self, get):
        response = Mock()
        response.json.return_value = {
            "addon": "core_mosquitto",
            "host": "core-mosquitto",
            "port": 1883,
            "username": "addons",
            "password": "supervisor-pass",
        }
        get.return_value = response

        settings, message = discover_mqtt_settings("token", self.fallback)

        self.assertEqual(settings["broker"], "core-mosquitto")
        self.assertEqual(settings["port"], 1883)
        self.assertEqual(settings["user"], "addons")
        self.assertEqual(settings["password"], "supervisor-pass")
        self.assertIn("core-mosquitto:1883", message)
        self.assertEqual(
            get.call_args.kwargs["headers"],
            {"Authorization": "Bearer token"},
        )

    @patch("bridge_helpers.requests.get")
    def test_uses_fallback_if_supervisor_service_is_missing(self, get):
        get.side_effect = requests.HTTPError(response=Mock(status_code=404))

        settings, message = discover_mqtt_settings("token", self.fallback)

        self.assertEqual(settings, self.fallback)
        self.assertIn("HTTP 404", message)

    @patch("bridge_helpers.requests.get")
    def test_uses_fallback_if_supervisor_token_is_missing(self, get):
        settings, message = discover_mqtt_settings("", self.fallback)

        self.assertEqual(settings, self.fallback)
        self.assertIn("TOKEN unavailable", message)
        get.assert_not_called()


if __name__ == "__main__":
    unittest.main()