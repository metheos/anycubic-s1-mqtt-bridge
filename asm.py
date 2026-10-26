import paho.mqtt.client as mqtt
import json
import logging
import os
import time
import signal
import sys
import ssl  # Add this import at the top of your file
from dotenv import load_dotenv
import requests
from bridge_helpers import (
    classify_mqtt_error,
    discover_mqtt_settings,
    get_firmware_version,
    get_color_box_id,
    global_filament_index,
    normalize_printer_device_name,
    normalize_box_slot_index,
    PRINT_SPEED_MODE_NAMES,
    print_speed_mode_name,
    translate_mqtt_error_message,
)
from threading import Thread, Event
import traceback
import random
from obico import (
    DEFAULT_OBICO_API_HOST,
    DEFAULT_OBICO_API_URL,
    build_print_job_state,
    build_api_url,
    check_api_health,
    detect_image_with_fallback,
    discover_obico_api_url,
    merge_print_data,
    printer_is_busy,
    resolve_print_job_state,
    resolve_printer_state,
    should_press_pause_button,
)

# Load configuration from .env file if present
load_dotenv()


# Function to get required environment variables
def get_required_env(name, default=None, required=False):
    value = os.getenv(name, default)
    if required and (value is None or value == default):
        raise ValueError(f"Required environment variable {name} is not set")
    return value


# Set up logging
log_level_name = get_required_env("LOG_LEVEL", "INFO").upper()
log_level = getattr(logging, log_level_name, logging.INFO)
logging.basicConfig(
    level=log_level, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)

logger = logging.getLogger("anycubic_mqtt_bridge")

# Application version
VERSION = "2026.02.08"

# Anycubic S1 IP Address - required
ANYCUBIC_S1_IP = get_required_env("ANYCUBIC_S1_IP", required=True)

# Home Assistant MQTT Broker - these are required
HA_BROKER = get_required_env("HA_BROKER", "homeassistant.local")
HA_PORT = int(get_required_env("HA_PORT", "1883"))
HA_USER = get_required_env("HA_USER")  # Optional
HA_PASS = get_required_env("HA_PASS")  # Optional
OBICO_ENABLED = get_required_env("OBICO_ENABLED", "false").lower() in (
    "true",
    "1",
    "yes",
    "on",
)
OBICO_API_URL = build_api_url(
    get_required_env("OBICO_API_URL", DEFAULT_OBICO_API_HOST)
)
SUPERVISOR_TOKEN = get_required_env("SUPERVISOR_TOKEN", "")
OBICO_DETECTION_INTERVAL = int(get_required_env("OBICO_DETECTION_INTERVAL", "30"))
OBICO_THRESHOLD = float(get_required_env("OBICO_THRESHOLD", "0.2"))

if OBICO_ENABLED and not OBICO_API_URL:
    raise ValueError("OBICO_API_URL is required when Obico ML is enabled")
if OBICO_DETECTION_INTERVAL < 1:
    raise ValueError("OBICO_DETECTION_INTERVAL must be at least 1 second")
if not 0 <= OBICO_THRESHOLD <= 1:
    raise ValueError("OBICO_THRESHOLD must be between 0 and 1")

# MQTT Configuration
KEEPALIVE_INTERVAL = int(get_required_env("KEEPALIVE_INTERVAL", "60"))

# Discovery and reconnection configuration
INITIAL_DISCOVERY_RETRY_DELAY = int(
    get_required_env("INITIAL_DISCOVERY_RETRY_DELAY", "5")
)
CONNECTION_CHECK_INTERVAL = int(get_required_env("CONNECTION_CHECK_INTERVAL", "30"))

# Global flag for controlling the main loop
running = True


class ConnectionState:
    """Enum-like class to track connection states"""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


class AnycubicMqttBridge:
    def __init__(self):
        self.anycubic_client = None
        self.ha_client = None
        self.stream_url = None
        self.current_print_state = "unknown"  # Add this attribute to store the current print state


        # One-time migration flag to avoid repeated retained cleanup
        self._cleanup_flag_file = os.path.join(os.path.dirname(__file__), ".ha_discovery_cleanup_done")

        # Different snapshot intervals based on printer state
        self.snapshot_interval_idle = int(
            get_required_env("SNAPSHOT_INTERVAL_IDLE", "300")
        )  # 5 minutes when idle
        self.snapshot_interval_busy = int(
            get_required_env("SNAPSHOT_INTERVAL_BUSY", "30")
        )  # 30 seconds when printing/etc
        self.snapshot_interval = (
            self.snapshot_interval_idle
        )  # Default to idle interval initially

        self.info_update_interval = int(get_required_env("INFO_UPDATE_INTERVAL", "30"))
        self.snapshot_thread = None
        self.obico_thread = None
        self.obico_entities_created = False
        self.obico_detection_interval = OBICO_DETECTION_INTERVAL
        self.obico_threshold = OBICO_THRESHOLD
        self.obico_pause_latched = False
        self.obico_active = Event()
        if OBICO_ENABLED:
            self.obico_active.set()
        self.info_thread = None
        self.connection_monitor_thread = None
        self.printer_state = "unknown"  # Track the printer state

        # Rest of the initialization remains the same...

        # Connection state tracking
        self.ha_connection_state = ConnectionState.DISCONNECTED
        self.anycubic_connection_state = ConnectionState.DISCONNECTED
        self.connectivity_entity_created = False
        self.last_reconnect_attempt = 0
        self.reconnect_delay = 1  # Initial delay in seconds
        self.connection_healthy = Event()  # Event to signal healthy connection
        self.snapshot_enabled = Event()  # Event to control snapshot worker
        self.discovered_color_box_ids = set()
        self.discovered_color_box_firmware = {}
        self.latest_color_box_data = {}
        self.legacy_color_box_cleanup_done = False

        # Initialize with empty values - we'll get them from discovery or env vars
        self.printer_mode_id = get_required_env("PRINTER_MODE_ID", None)
        self.printer_device_id = get_required_env("PRINTER_DEVICE_ID", None)
        self.printer_model = get_required_env("PRINTER_MODEL", None)
        self.printer_cn = get_required_env("PRINTER_CN", None)  # Add printer_cn field

        # Try to discover the printer with retry logic
        self.discover_printer_with_retry()

        # If discovery failed and we don't have all required parameters, we can't proceed
        if not self.printer_mode_id or not self.printer_device_id:
            logger.error(
                "Printer discovery failed and required printer parameters are not provided in environment variables"
            )
            raise ValueError("Cannot establish connection: Missing printer information")

        logger.info(
            f"Using printer configuration: Model={self.printer_model}, ModeID={self.printer_mode_id}, DeviceID={self.printer_device_id}"
        )

        self.ha_mqtt_settings, mqtt_lookup_result = discover_mqtt_settings(
            SUPERVISOR_TOKEN,
            {
                "broker": HA_BROKER,
                "port": HA_PORT,
                "user": HA_USER,
                "password": HA_PASS,
            },
        )
        logger.info("Home Assistant MQTT lookup: %s", mqtt_lookup_result)

        self.setup_clients()

    def get_device_info(self):
        """Return consistent device info dictionary for Home Assistant entities"""
        device_id = self.printer_cn if self.printer_cn else "anycubic_printer"
        model = getattr(self, "device_model", None)
        if not model or model == "Unknown":
            model = getattr(self, "printer_model", None) or "Unknown"
        device_info = {
            "identifiers": [device_id],
            "name": normalize_printer_device_name(
                getattr(self, "device_name", None), model
            ),
            "model": model,
            "manufacturer": "Anycubic",
        }
        firmware = get_firmware_version(
            {"version": getattr(self, "printer_version", None)}, ("version",)
        )
        if firmware:
            device_info["sw_version"] = firmware
        if self.printer_cn:
            device_info["serial_number"] = self.printer_cn
        return device_info

    def get_color_box_device_info(self, box_index, firmware=None):
        printer_device_id = self.get_device_info()["identifiers"][0]
        box_number = int(box_index)
        device_info = {
            "identifiers": [f"{printer_device_id}_ace_box_{box_index}"],
            "name": f"ACE Pro Box {box_number}",
            "manufacturer": "Anycubic",
            "model": "ACE Pro Filament System",
            "via_device": printer_device_id,
        }
        firmware = get_firmware_version({"firmware": firmware}, ("firmware",))
        if firmware:
            device_info["sw_version"] = firmware
        return device_info

    def discover_printer_with_retry(self):
        """Discover the printer with exponential backoff retry, continuing indefinitely"""
        delay = INITIAL_DISCOVERY_RETRY_DELAY
        attempts = 0
        max_backoff_delay = 300  # Cap backoff at 5 minutes between attempts

        # Keep trying indefinitely until we succeed or the program is terminated
        while running:
            attempts += 1
            logger.info(f"Printer discovery attempt {attempts}")

            if self.discover_printer():
                logger.info("Printer discovery successful")
                return True

            # If we have environment variables as fallback, use them but keep retrying discovery
            if self.printer_mode_id and self.printer_device_id and attempts == 1:
                logger.info(
                    "Using printer parameters from environment variables while discovery continues"
                )
                # Don't return here - we'll keep trying discovery in the background

            logger.info(f"Discovery failed, retrying in {delay:.1f} seconds...")
            time.sleep(delay)

            # Exponential backoff with jitter, but cap at max_backoff_delay
            delay = min(max_backoff_delay, delay * 1.5) * (0.8 + 0.4 * random.random())

        # This will only be reached if running is set to False during discovery
        logger.warning("Discovery terminated by shutdown signal")
        return False

    def setup_clients(self):
        """Set up MQTT clients for Anycubic and Home Assistant"""
        # Set up Anycubic MQTT Client
        self.anycubic_client = mqtt.Client(client_id="anycubic_bridge")
        self.anycubic_client.username_pw_set(ANYCUBIC_USER, ANYCUBIC_PASS)

        # Modified TLS settings to accept self-signed certificates
        self.anycubic_client.tls_set(cert_reqs=ssl.CERT_NONE)

        self.anycubic_client.on_connect = self.on_anycubic_connect
        self.anycubic_client.on_disconnect = self.on_anycubic_disconnect
        self.anycubic_client.on_message = self.on_anycubic_message

        # Set up Home Assistant MQTT Client
        self.ha_client = mqtt.Client(client_id="anycubic_bridge_ha")
        if self.ha_mqtt_settings["user"] and self.ha_mqtt_settings["password"]:
            self.ha_client.username_pw_set(
                self.ha_mqtt_settings["user"], self.ha_mqtt_settings["password"]
            )
        self.ha_client.on_connect = self.on_ha_connect
        self.ha_client.on_disconnect = self.on_ha_disconnect

    def connect(self):
        """Connect to both brokers with exponential backoff"""
        if not self.printer_device_id or not self.printer_mode_id:
            logger.error("Cannot connect: Missing printer device ID or mode ID")
            return False

        # Reset connection states
        self.ha_connection_state = ConnectionState.CONNECTING
        self.anycubic_connection_state = ConnectionState.CONNECTING
        self.update_connectivity_status()

        backoff_time = 1  # Initial backoff time in seconds
        max_backoff_time = 60  # Maximum backoff time in seconds

        while running:
            try:
                logger.info(
                    "Connecting to Home Assistant broker at %s:%s",
                    self.ha_mqtt_settings["broker"],
                    self.ha_mqtt_settings["port"],
                )
                self.ha_client.connect(
                    self.ha_mqtt_settings["broker"],
                    self.ha_mqtt_settings["port"],
                    keepalive=KEEPALIVE_INTERVAL,
                )
                self.ha_client.loop_start()

                logger.info(
                    f"Connecting to Anycubic broker at {ANYCUBIC_BROKER}:{ANYCUBIC_PORT}"
                )
                logger.info(f"Using printer device ID: {self.printer_device_id}")
                self.anycubic_client.connect(
                    ANYCUBIC_BROKER, ANYCUBIC_PORT, keepalive=KEEPALIVE_INTERVAL
                )
                self.anycubic_client.loop_start()

                # Reset backoff parameters on successful connection
                self.reconnect_delay = 1
                self.connection_healthy.set()

                # Start the info update thread
                self.info_thread = Thread(target=self.info_update_worker)
                self.info_thread.daemon = True
                self.info_thread.start()
                logger.info(
                    f"Started info update thread with interval {self.info_update_interval}s"
                )

                # Start connection monitoring thread
                self.start_connection_monitor()

                return True
            except Exception as e:
                logger.error(f"Failed to connect: {e}")
                self.ha_connection_state = ConnectionState.ERROR
                self.anycubic_connection_state = ConnectionState.ERROR
                self.update_connectivity_status()

                logger.info(f"Retrying in {backoff_time} seconds...")
                time.sleep(backoff_time)
                backoff_time = min(max_backoff_time, backoff_time * 2) * (
                    0.8 + 0.4 * random.random()
                )

        return False

    def start_connection_monitor(self):
        """Start a thread to periodically check connection health"""
        if (
            self.connection_monitor_thread is None
            or not self.connection_monitor_thread.is_alive()
        ):
            self.connection_monitor_thread = Thread(
                target=self.connection_monitor_worker
            )
            self.connection_monitor_thread.daemon = True
            self.connection_monitor_thread.start()
            logger.info(
                f"Started connection monitor thread with interval {CONNECTION_CHECK_INTERVAL}s"
            )

    def connection_monitor_worker(self):
        """Background worker that monitors connection health"""
        while running:
            try:
                # Check if connections are active
                ha_connected = self.ha_client.is_connected()
                anycubic_connected = self.anycubic_client.is_connected()

                # Update connection states
                if ha_connected:
                    if self.ha_connection_state != ConnectionState.CONNECTED:
                        logger.info("Home Assistant connection is healthy")
                        self.ha_connection_state = ConnectionState.CONNECTED
                else:
                    if self.ha_connection_state == ConnectionState.CONNECTED:
                        logger.warning("Lost connection to Home Assistant")
                        self.ha_connection_state = ConnectionState.DISCONNECTED

                if anycubic_connected:
                    if self.anycubic_connection_state != ConnectionState.CONNECTED:
                        logger.info("Anycubic connection is healthy")
                        self.anycubic_connection_state = ConnectionState.CONNECTED
                        # Enable snapshot worker when printer is connected
                        if self.stream_url:
                            self.snapshot_enabled.set()
                            logger.info("Snapshot worker enabled - printer connected")
                else:
                    if self.anycubic_connection_state == ConnectionState.CONNECTED:
                        logger.warning("Lost connection to Anycubic")
                        self.anycubic_connection_state = ConnectionState.DISCONNECTED
                        # Disable snapshot worker when printer disconnects
                        self.snapshot_enabled.clear()
                        logger.info("Snapshot worker paused - printer disconnected")

                # Update status in Home Assistant
                self.update_connectivity_status()

                # If either connection is down, try to reconnect
                current_time = time.time()
                if (not ha_connected or not anycubic_connected) and (
                    current_time - self.last_reconnect_attempt
                ) > self.reconnect_delay:
                    logger.info(
                        "Connection monitor detected disconnect, attempting reconnection"
                    )
                    self.last_reconnect_attempt = current_time

                    # Attempt reconnection with exponential backoff
                    if not ha_connected:
                        try:
                            logger.info("Attempting to reconnect to Home Assistant")
                            self.ha_client.reconnect()
                        except Exception as e:
                            logger.error(f"Failed to reconnect to Home Assistant: {e}")

                    if not anycubic_connected:
                        try:
                            logger.info("Attempting to reconnect to Anycubic")
                            self.anycubic_client.reconnect()

                            # Re-subscribe and request info after reconnection
                            if self.anycubic_client.is_connected():
                                self.subscribe_to_anycubic_topics()
                                self.request_printer_info()
                        except Exception as e:
                            logger.error(f"Failed to reconnect to Anycubic: {e}")

                    # Increase backoff delay for next attempt
                    self.reconnect_delay = min(300, self.reconnect_delay * 2) * (
                        0.8 + 0.4 * random.random()
                    )

                # If both connections are healthy, reset backoff
                if ha_connected and anycubic_connected:
                    self.reconnect_delay = 1
                    self.connection_healthy.set()
                else:
                    self.connection_healthy.clear()

            except Exception as e:
                logger.error(f"Error in connection monitor: {e}")

            # Sleep until next check
            time.sleep(CONNECTION_CHECK_INTERVAL)

    def get_topic_prefix(self):
        """Get the MQTT topic prefix including printer CN if available"""
        # Use printer CN/serial to create unique topics
        if hasattr(self, "printer_cn") and self.printer_cn:
            return f"anycubic_{self.printer_cn.lower().replace('-', '_')}"
        return "anycubic_printer"  # Default if no CN is available

    def update_connectivity_status(self):
        """Update connectivity status in Home Assistant"""
        if not hasattr(self, "ha_client") or self.ha_client is None:
            return

        try:
            # Get topic prefix
            topic_prefix = self.get_topic_prefix()

            # Create connectivity sensor if it doesn't exist
            if not self.connectivity_entity_created and self.ha_client.is_connected():
                self.create_connectivity_entity()

            # Determine overall status
            if self.anycubic_connection_state == ConnectionState.CONNECTED:
                status = "online"
                status_detail = "Printer is online and connected"
            elif self.anycubic_connection_state == ConnectionState.CONNECTING:
                status = "connecting"
                status_detail = "Attempting to connect to printer"
            elif self.anycubic_connection_state == ConnectionState.ERROR:
                status = "error"
                status_detail = "Error connecting to printer"
            else:  # DISCONNECTED
                status = "offline"
                status_detail = "Printer is offline"

            # Update status in Home Assistant
            if self.ha_client.is_connected():
                status_data = {
                    "state": status,
                    "ha_connection": self.ha_connection_state,
                    "anycubic_connection": self.anycubic_connection_state,
                    "detail": status_detail,
                    "last_updated": time.strftime("%Y-%m-%d %H:%M:%S"),
                }

                # Use the printer-specific topic
                self.ha_client.publish(
                    f"homeassistant/sensor/{topic_prefix}_connectivity/state",
                    json.dumps(status_data),
                    retain=True,
                )
        except Exception as e:
            logger.error(f"Error updating connectivity status: {e}")

    def create_connectivity_entity(self):
        """Create connectivity sensor in Home Assistant"""
        try:
            # Get topic prefix and device info
            topic_prefix = self.get_topic_prefix()
            device_info = self.get_device_info()

            # Create connectivity sensor with unique topic
            connectivity_config = {
                "name": "Printer Connectivity",
                "unique_id": f"{topic_prefix}_connectivity",
                "state_topic": f"homeassistant/sensor/{topic_prefix}_connectivity/state",
                "value_template": "{{ value_json.state }}",
                "availability_topic": f"homeassistant/sensor/{topic_prefix}_connectivity/availability",
                "json_attributes_topic": f"homeassistant/sensor/{topic_prefix}_connectivity/state",
                "icon": "mdi:network",
                "entity_category": "diagnostic",
                "device": device_info,
            }

            # Publish configuration
            self.ha_client.publish(
                f"homeassistant/sensor/{topic_prefix}_connectivity/config",
                json.dumps(connectivity_config),
                retain=True,
            )

            # Mark entity as available
            self.ha_client.publish(
                f"homeassistant/sensor/{topic_prefix}_connectivity/availability",
                "online",
                retain=True,
            )

            self.connectivity_entity_created = True
            logger.info("Created printer connectivity entity in Home Assistant")
        except Exception as e:
            logger.error(f"Error creating connectivity entity: {e}")

    def disconnect(self):
        """Disconnect from both brokers"""
        logger.info("Disconnecting from brokers")
        if self.ha_client:
            try:
                self.ha_client.loop_stop()
                self.ha_client.disconnect()
                self.ha_connection_state = ConnectionState.DISCONNECTED
            except Exception as e:
                logger.error(f"Error disconnecting from Home Assistant: {e}")

        if self.anycubic_client:
            try:
                self.anycubic_client.loop_stop()
                self.anycubic_client.disconnect()
                self.anycubic_connection_state = ConnectionState.DISCONNECTED
            except Exception as e:
                logger.error(f"Error disconnecting from Anycubic: {e}")

        # Final status update
        self.update_connectivity_status()

    def on_anycubic_connect(self, client, userdata, flags, rc):
        """Handle connection to Anycubic broker"""
        if rc == 0:
            logger.info("Connected to Anycubic MQTT broker")
            self.anycubic_connection_state = ConnectionState.CONNECTED
            self.update_connectivity_status()

            # Enable snapshots if stream URL is available
            if self.stream_url:
                self.snapshot_enabled.set()
                logger.info("Snapshot worker enabled - connection established")

            # Subscribe to topics
            self.subscribe_to_anycubic_topics()

            # Request printer information after connecting
            self.request_printer_info()

            # Also request light status
            self._query_light_status()

            # Create print controls
            if not hasattr(self, "print_controls_created") or not self.print_controls_created:
                self.print_controls_created = self._create_print_controls()
        else:
            logger.error(f"Failed to connect to Anycubic broker, return code: {rc}")
            self.anycubic_connection_state = ConnectionState.ERROR
            self.update_connectivity_status()

    def subscribe_to_anycubic_topics(self):
        """Subscribe to all required Anycubic topics"""
        if not self.printer_mode_id or not self.printer_device_id:
            logger.error("Cannot subscribe: Missing printer mode ID or device ID")
            return False

        try:
            # Subscribe to printer and light
            self.anycubic_client.subscribe(
                f"anycubic/anycubicCloud/v1/printer/+/{self.printer_mode_id}/{self.printer_device_id}/#"
            )
            logger.info("Subscribed to Anycubic printer topics")

            # Subscribe to camera
            self.anycubic_client.subscribe(
                f"anycubic/anycubicCloud/v1/+/public/{self.printer_mode_id}/{self.printer_device_id}/+/report"
            )
            return True
        except Exception as e:
            logger.error(f"Error subscribing to Anycubic topics: {e}")
            return False

    def _query_light_status(self):
        """Request light status from printer"""

        if not self.printer_mode_id or not self.printer_device_id:

            logger.warning("Cannot query light: Missing printer mode ID or device ID")

            return False

        try:

            import uuid

            # Create message ID

            message_id = str(uuid.uuid4())

            # Format the topic

            topic = f"anycubic/anycubicCloud/v1/web/printer/{self.printer_mode_id}/{self.printer_device_id}/light"

            # Create request payload

            light_request = {
                "type": "light",
                "action": "query",
                "timestamp": int(time.time() * 1000),
                "msgid": message_id,
                "data": None,
            }

            # Send the request

            logger.info("Requesting printer light status")

            self.anycubic_client.publish(topic, json.dumps(light_request))

            return True

        except Exception as e:

            logger.error(f"Error querying printer light: {e}")

            return False

    def request_printer_info(self):
        """Request printer information"""
        import uuid

        # Validate we have required parameters
        if not self.printer_device_id or not self.printer_mode_id:
            logger.error("Cannot request printer info: Missing device ID or mode ID")
            return

        # Log a single consolidated message at INFO level
        logger.info("Requesting printer status updates (info, light, multicolor box)")
        
        # Create message IDs and payloads for all requests
        message_id = str(uuid.uuid4())
        message_id_light = str(uuid.uuid4()) 
        message_id_box = str(uuid.uuid4())
        
        # Base topic
        base_topic = f"anycubic/anycubicCloud/v1/web/printer/{self.printer_mode_id}/{self.printer_device_id}"
        
        # Create and send info request
        info_request = {
            "type": "info",
            "action": "query",
            "timestamp": int(time.time() * 1000),
            "msgid": message_id,
            "data": None,
        }
        self.anycubic_client.publish(f"{base_topic}/info", json.dumps(info_request))
        
        # Log details at DEBUG level only
        logger.debug(f"Sent info request with message ID: {message_id}")
        
        # Create and send light request
        info_request_light = {
            "type": "light",
            "action": "query",
            "timestamp": int(time.time() * 1000),
            "msgid": message_id_light,
            "data": None,
        }
        self.anycubic_client.publish(f"{base_topic}/light", json.dumps(info_request_light))
        logger.debug(f"Sent light request with message ID: {message_id_light}")
        
        # Create and send multicolor box request
        info_request_box = {
            "type": "multiColorBox",
            "action": "getInfo",
            "timestamp": int(time.time() * 1000),
            "msgid": message_id_box,
            "data": None,
        }
        self.anycubic_client.publish(f"{base_topic}/multiColorBox", json.dumps(info_request_box))
        logger.debug(f"Sent multicolor box request with message ID: {message_id_box}")

    def on_anycubic_disconnect(self, client, userdata, rc):
        """Handle disconnection from Anycubic broker"""
        logger.warning(f"Disconnected from Anycubic broker with code {rc}")
        self.anycubic_connection_state = ConnectionState.DISCONNECTED
        self.update_connectivity_status()
        self.connection_healthy.clear()

        # Disable snapshots when printer disconnects
        self.snapshot_enabled.clear()
        logger.info("Snapshot worker paused - disconnected from printer")

        # Let the connection monitor handle reconnection with backoff

    def on_ha_connect(self, client, userdata, flags, rc):
        """Handle connection to Home Assistant broker"""
        if rc == 0:
            logger.info("Connected to Home Assistant MQTT broker")
            self.ha_connection_state = ConnectionState.CONNECTED
            self.update_connectivity_status()
            if not self.legacy_color_box_cleanup_done:
                self.cleanup_legacy_color_box_entities()
                self.legacy_color_box_cleanup_done = True
            # Run retained cleanup only once per installation
            try:
                if not self._has_migration_cleanup_done():
                    # Clear any old retained discovery configs that may cause HA errors
                    self.cleanup_old_discovery_entities()
                    self._mark_migration_cleanup_done()
            except Exception as e:
                logger.debug(f"Cleanup flag check failed: {e}")
            self.print_controls_created = self._create_print_controls()
            if OBICO_ENABLED:
                self._create_obico_entities()
                self._start_obico_worker()
            elif not OBICO_ENABLED:
                self._cleanup_obico_discovery_entities()
        else:
            logger.error(
                f"Failed to connect to Home Assistant broker, return code: {rc}"
            )
            self.ha_connection_state = ConnectionState.ERROR
            self.update_connectivity_status()
    def cleanup_old_discovery_entities(self):
        """Remove previously published, invalid MQTT discovery configs (retained)."""
        try:
            topic_prefix = self.get_topic_prefix()

            # Old invalid camera discovery (MQTT camera requires 'topic'; we use image entity instead)
            bad_camera_topic = f"homeassistant/camera/{topic_prefix}_printer_camera/config"
            self.ha_client.publish(bad_camera_topic, "", retain=True)

            # Old invalid filament color lights (missing command_topic). We now publish sensors instead.
            for i in range(4):
                bad_light_topic = f"homeassistant/light/{topic_prefix}_filament_{i}_color_light/config"
                self.ha_client.publish(bad_light_topic, "", retain=True)

            logger.info("Cleared old retained MQTT discovery entities (camera, filament lights)")
        except Exception as e:
            logger.warning(f"Failed to clear old discovery entities: {e}")

    def cleanup_legacy_color_box_entities(self):
        topic_prefix = self.get_topic_prefix()
        for unique_id in (
            f"{topic_prefix}_serial_number",
            f"{topic_prefix}_target_nozzle_temp",
            f"{topic_prefix}_target_hotbed_temp",
            f"{topic_prefix}_printer_camera_url",
        ):
            self.ha_client.publish(
                f"homeassistant/sensor/{unique_id}/config",
                "",
                retain=True,
            )

        for unique_id in (
            f"{topic_prefix}_color_box_status",
            f"{topic_prefix}_color_box_temp",
            f"{topic_prefix}_active_filament_slot",
            f"{topic_prefix}_fan_speed",
            f"{topic_prefix}_aux_fan_speed",
            f"{topic_prefix}_box_fan_level",
        ):
            self.ha_client.publish(
                f"homeassistant/sensor/{unique_id}/config",
                "",
                retain=True,
            )

        for box_index in range(8):
            for suffix in ("status", "temp", "active_filament_slot"):
                unique_id = f"{topic_prefix}_color_box_{box_index}_{suffix}"
                self.ha_client.publish(
                    f"homeassistant/sensor/{unique_id}/config",
                    "",
                    retain=True,
                )
            for slot_index in range(box_index * 4, (box_index + 1) * 4):
                for entity_type in ("type", "color"):
                    unique_id = f"{topic_prefix}_filament_{slot_index}_{entity_type}"
                    self.ha_client.publish(
                        f"homeassistant/sensor/{unique_id}/config",
                        "",
                        retain=True,
                    )

        for component, unique_id in (
            ("switch", f"{topic_prefix}_obico_communication"),
            ("number", f"{topic_prefix}_obico_threshold"),
            ("number", f"{topic_prefix}_obico_detection_interval"),
            ("binary_sensor", f"{topic_prefix}_obico_failure"),
            ("sensor", f"{topic_prefix}_obico_confidence"),
            ("image", f"{topic_prefix}_obico_detection"),
        ):
            self.ha_client.publish(
                f"homeassistant/{component}/{unique_id}/config",
                "",
                retain=True,
            )

        for slot_index in range(8):
            for entity_type in ("type", "color"):
                self.ha_client.publish(
                    f"homeassistant/sensor/{topic_prefix}_filament_{slot_index}_{entity_type}/config",
                    "",
                    retain=True,
                )
                for old_box_index in range(8):
                    self.ha_client.publish(
                        f"homeassistant/sensor/{topic_prefix}_color_box_{old_box_index}_filament_{slot_index}_{entity_type}/config",
                        "",
                        retain=True,
                    )

    def _has_migration_cleanup_done(self):
        try:
            return os.path.isfile(self._cleanup_flag_file)
        except Exception:
            return False

    def _mark_migration_cleanup_done(self):
        try:
            with open(self._cleanup_flag_file, "w", encoding="utf-8") as f:
                f.write("done")
        except Exception:
            pass

    def _cleanup_obico_discovery_entities(self):
        topic_prefix = self.get_topic_prefix()
        for component, unique_id in (
            ("switch", f"{topic_prefix}_obico_communication"),
            ("number", f"{topic_prefix}_obico_threshold"),
            ("binary_sensor", f"{topic_prefix}_obico_failure"),
            ("sensor", f"{topic_prefix}_obico_confidence"),
            ("image", f"{topic_prefix}_obico_detection"),
        ):
            self.ha_client.publish(
                f"homeassistant/{component}/{unique_id}/config", "", retain=True
            )
        for topic in (
            f"homeassistant/sensor/{topic_prefix}/obico",
            f"homeassistant/sensor/{topic_prefix}/obico/availability",
            f"homeassistant/sensor/{topic_prefix}/obico/image",
            f"homeassistant/sensor/{topic_prefix}/obico/image_state",
            f"homeassistant/switch/{topic_prefix}_obico/state",
            f"homeassistant/number/{topic_prefix}_obico_threshold/state",
            f"homeassistant/number/{topic_prefix}_obico_threshold/set",
            f"homeassistant/number/{topic_prefix}_obico_detection_interval/state",
            f"homeassistant/number/{topic_prefix}_obico_detection_interval/set",
        ):
            self.ha_client.publish(topic, "", retain=True)

    def _create_obico_entities(self):
        if self.obico_entities_created or not self.ha_client.is_connected():
            return

        topic_prefix = self.get_topic_prefix()
        printer_device_info = self.get_device_info()
        printer_device_id = printer_device_info["identifiers"][0]
        device_info = {
            "identifiers": [f"{printer_device_id}_obico"],
            "name": "Obico ML",
            "manufacturer": "Obico",
            "model": "Print Failure Detection",
            "via_device": printer_device_id,
        }
        state_topic = f"homeassistant/sensor/{topic_prefix}/obico"
        availability_topic = f"homeassistant/sensor/{topic_prefix}/obico/availability"
        switch_state_topic = f"homeassistant/switch/{topic_prefix}_obico/state"
        switch_command_topic = f"homeassistant/switch/{topic_prefix}_obico/set"
        threshold_state_topic = f"homeassistant/number/{topic_prefix}_obico_threshold/state"
        threshold_command_topic = f"homeassistant/number/{topic_prefix}_obico_threshold/set"
        interval_state_topic = f"homeassistant/number/{topic_prefix}_obico_detection_interval/state"
        interval_command_topic = f"homeassistant/number/{topic_prefix}_obico_detection_interval/set"
        entities = (
            (
                "switch",
                f"{topic_prefix}_obico_communication",
                {
                    "name": "Obico ML Communication",
                    "state_topic": switch_state_topic,
                    "command_topic": switch_command_topic,
                    "payload_on": "ON",
                    "payload_off": "OFF",
                    "state_on": "ON",
                    "state_off": "OFF",
                    "availability_topic": availability_topic,
                    "icon": "mdi:robot",
                    "device": device_info,
                },
            ),
            (
                "number",
                f"{topic_prefix}_obico_threshold",
                {
                    "name": "Obico ML Detection Threshold",
                    "state_topic": threshold_state_topic,
                    "command_topic": threshold_command_topic,
                    "min": 0.0,
                    "max": 1.0,
                    "step": 0.01,
                    "mode": "slider",
                    "retain": True,
                    "availability_topic": availability_topic,
                    "icon": "mdi:tune-variant",
                    "device": device_info,
                },
            ),
            (
                "number",
                f"{topic_prefix}_obico_detection_interval",
                {
                    "name": "Obico ML Detection Interval",
                    "state_topic": interval_state_topic,
                    "command_topic": interval_command_topic,
                    "min": 1,
                    "max": 3600,
                    "step": 1,
                    "mode": "slider",
                    "unit_of_measurement": "s",
                    "retain": True,
                    "availability_topic": availability_topic,
                    "icon": "mdi:timer-cog-outline",
                    "device": device_info,
                },
            ),
            (
                "binary_sensor",
                f"{topic_prefix}_obico_failure",
                {
                    "name": "Obico ML Failure Detected",
                    "state_topic": state_topic,
                    "value_template": "{{ 'ON' if value_json.error_detected else 'OFF' }}",
                    "payload_on": "ON",
                    "payload_off": "OFF",
                    "device_class": "problem",
                    "availability_topic": availability_topic,
                    "device": device_info,
                },
            ),
            (
                "sensor",
                f"{topic_prefix}_obico_confidence",
                {
                    "name": "Obico ML Failure Confidence",
                    "state_topic": state_topic,
                    "value_template": "{{ value_json.avg_confidence }}",
                    "unit_of_measurement": "%",
                    "state_class": "measurement",
                    "availability_topic": availability_topic,
                    "icon": "mdi:chart-line",
                    "device": device_info,
                },
            ),
            (
                "image",
                f"{topic_prefix}_obico_detection",
                {
                    "name": "Obico ML Detection Image",
                    "state_topic": f"{state_topic}/image_state",
                    "image_topic": f"{state_topic}/image",
                    "content_type": "image/jpeg",
                    "availability_topic": availability_topic,
                    "device": device_info,
                },
            ),
        )

        for component, unique_id, config in entities:
            config["unique_id"] = unique_id
            self.ha_client.publish(
                f"homeassistant/{component}/{unique_id}/config",
                json.dumps(config),
                retain=True,
            )

        self.ha_client.subscribe(switch_command_topic)
        self.ha_client.message_callback_add(
            switch_command_topic, self._on_obico_switch_command
        )
        self.ha_client.message_callback_add(
            threshold_command_topic, self._on_obico_threshold_command
        )
        self.ha_client.subscribe(threshold_command_topic)
        self.ha_client.message_callback_add(
            interval_command_topic, self._on_obico_interval_command
        )
        self.ha_client.subscribe(interval_command_topic)
        self.ha_client.publish(
            switch_state_topic,
            "ON" if self.obico_active.is_set() else "OFF",
            retain=True,
        )
        self.ha_client.publish(
            threshold_state_topic,
            f"{self.obico_threshold:g}",
            retain=True,
        )
        self.ha_client.publish(
            interval_state_topic,
            str(self.obico_detection_interval),
            retain=True,
        )
        self.ha_client.publish(availability_topic, "offline", retain=True)
        self.obico_entities_created = True
        logger.info("Created Obico ML entities in Home Assistant")

    def _on_obico_switch_command(self, client, userdata, msg):
        payload = msg.payload.decode("utf-8").strip().upper()
        if payload == "ON":
            self.obico_active.set()
        elif payload == "OFF":
            self.obico_active.clear()
        else:
            logger.warning("Ignoring invalid Obico switch value: %s", payload)
            return
        client.publish(msg.topic.rsplit("/", 1)[0] + "/state", payload, retain=True)

    def _on_obico_threshold_command(self, client, userdata, msg):
        try:
            threshold = float(msg.payload.decode("utf-8").strip())
        except (UnicodeDecodeError, ValueError):
            logger.warning("Ignoring invalid Obico threshold value: %r", msg.payload)
            return

        if not 0.0 <= threshold <= 1.0:
            logger.warning("Ignoring out-of-range Obico threshold: %s", threshold)
            return

        self.obico_threshold = threshold
        topic = f"homeassistant/number/{self.get_topic_prefix()}_obico_threshold/state"
        self.ha_client.publish(topic, f"{threshold:g}", retain=True)
        logger.info("Updated Obico ML detection threshold to %.2f", threshold)

    def _on_obico_interval_command(self, client, userdata, msg):
        try:
            requested = float(msg.payload.decode("utf-8").strip())
            if not requested.is_integer():
                raise ValueError("interval must be a whole number")
            interval = int(requested)
        except (UnicodeDecodeError, ValueError):
            logger.warning("Ignoring invalid Obico interval value: %r", msg.payload)
            return

        if not 1 <= interval <= 3600:
            logger.warning("Ignoring out-of-range Obico interval: %s", interval)
            return

        self.obico_detection_interval = interval
        topic = (
            f"homeassistant/number/{self.get_topic_prefix()}"
            "_obico_detection_interval/state"
        )
        self.ha_client.publish(topic, str(interval), retain=True)
        logger.info("Updated Obico ML detection interval to %s seconds", interval)

    def _start_obico_worker(self):
        if not OBICO_ENABLED:
            return
        if self.obico_thread is None or not self.obico_thread.is_alive():
            self.obico_thread = Thread(target=self.obico_worker, daemon=True)
            self.obico_thread.start()
            logger.info(
                "Started Obico ML worker with interval %ss",
                self.obico_detection_interval,
            )

    def obico_worker(self):
        topic_prefix = self.get_topic_prefix()
        state_topic = f"homeassistant/sensor/{topic_prefix}/obico"
        availability_topic = f"{state_topic}/availability"
        next_detection = 0
        next_health_check = 0
        api_available = False
        previous_health_state = None
        previous_discovery_result = None
        api_url = OBICO_API_URL
        prefer_fallback_api = False

        while running:
            current_time = time.monotonic()
            if not printer_is_busy(self.printer_state):
                self.obico_pause_latched = False

            if current_time >= next_health_check:
                if previous_discovery_result is None:
                    logger.info(
                        "Searching Home Assistant Supervisor for the installed Obico ML add-on"
                    )
                discovered_api_url, discovery_result = discover_obico_api_url(
                    SUPERVISOR_TOKEN, OBICO_API_URL
                )
                if discovery_result != previous_discovery_result:
                    logger.info("Supervisor Obico lookup: %s", discovery_result)
                    previous_discovery_result = discovery_result
                if prefer_fallback_api:
                    discovered_api_url = OBICO_API_URL
                if discovered_api_url != api_url:
                    api_url = discovered_api_url
                    logger.info("Using Obico ML API at %s", api_url)
                api_available = check_api_health(api_url)
                health_state = "online" if api_available else "offline"
                self.ha_client.publish(
                    availability_topic, health_state, retain=True
                )
                if health_state != previous_health_state:
                    if api_available:
                        logger.info("Obico ML API is reachable at %s", api_url)
                    else:
                        logger.warning(
                            "Obico ML API is unavailable at %s; install and start the Obico ML add-on",
                            api_url,
                        )
                    previous_health_state = health_state
                next_health_check = current_time + 30

            if (
                not api_available
                or
                not self.obico_active.is_set()
                or not self.snapshot_enabled.is_set()
                or not printer_is_busy(self.printer_state)
                or current_time < next_detection
            ):
                time.sleep(1)
                continue

            try:
                image_data = self.take_snapshot()
                if not image_data:
                    logger.warning("Obico ML skipped: printer snapshot unavailable")
                else:
                    logger.info(
                        "Obico ML sending snapshot: bytes=%d jpeg=%s threshold=%.2f",
                        len(image_data),
                        image_data.startswith(b"\xff\xd8"),
                        self.obico_threshold,
                    )
                    result, api_url, used_fallback = detect_image_with_fallback(
                        image_data,
                        api_url,
                        OBICO_API_URL,
                        self.obico_threshold,
                    )
                    if used_fallback:
                        prefer_fallback_api = True
                        logger.warning(
                            "Obico ML auto-discovered hostname failed to connect; switched to fallback API at %s",
                            api_url,
                        )
                    self.ha_client.publish(
                        state_topic,
                        json.dumps(
                            {
                                "error_detected": result["error_detected"],
                                "avg_confidence": result["avg_confidence"],
                                "last_updated": time.strftime("%Y-%m-%d %H:%M:%S"),
                            }
                        ),
                        retain=True,
                    )
                    detection_image = result["image_with_errors"] or image_data
                    self.ha_client.publish(
                        f"{state_topic}/image", detection_image, retain=True
                    )
                    self.ha_client.publish(
                        f"{state_topic}/image_state",
                        time.strftime("%Y-%m-%d %H:%M:%S"),
                        retain=True,
                    )
                    logger.info(
                        "Obico ML result: detections=%d failure=%s confidence=%s%%",
                        result["detection_count"],
                        result["error_detected"],
                        result["avg_confidence"],
                    )
                    if should_press_pause_button(
                        result["error_detected"],
                        self.printer_state,
                        self.obico_pause_latched,
                    ):
                        pause_command_topic = (
                            f"homeassistant/button/{topic_prefix}_pause_print/command"
                        )
                        self.ha_client.publish(pause_command_topic, "PRESS")
                        self.obico_pause_latched = True
                        logger.warning(
                            "Obico ML detected a failure; pressing the Pause Print button"
                        )
                    elif not result["error_detected"]:
                        self.obico_pause_latched = False
            except Exception as e:
                logger.warning("Obico ML detection failed: %s", e)

            next_detection = time.monotonic() + self.obico_detection_interval

    def on_ha_disconnect(self, client, userdata, rc):
        """Handle disconnection from Home Assistant broker"""
        logger.warning(f"Disconnected from Home Assistant broker with code {rc}")
        self.ha_connection_state = ConnectionState.DISCONNECTED
        self.update_connectivity_status()
        self.connection_healthy.clear()
        self.print_controls_created = False
        self.temperature_fan_controls_created = False
        self.obico_entities_created = False

        # Let the connection monitor handle reconnection with backoff

    def start_video_capture(self):
        """Send command to start video capture on the printer"""
        if not self.printer_mode_id or not self.printer_device_id:
            logger.warning(
                "Cannot start video capture: Missing printer mode ID or device ID"
            )
            return False

        try:
            import uuid

            # Create message ID
            message_id = str(uuid.uuid4())

            # Format the topic
            topic = f"anycubic/anycubicCloud/v1/web/printer/{self.printer_mode_id}/{self.printer_device_id}/video"
            response_topic = f"anycubic/anycubicCloud/v1/printer/public/{self.printer_mode_id}/{self.printer_device_id}/video/report"

            # Create request payload
            capture_request = {
                "type": "video",
                "action": "startCapture",
                "timestamp": int(time.time() * 1000),
                "msgid": message_id,
                "data": None,
            }

            # Create a class attribute to track initialization success
            self.video_init_success = False
            self.video_init_message_id = message_id

            # Register a callback in the main handler
            def video_response_handler(topic, payload):
                print(f"Received response: {topic} -> {payload}")
                if (
                    topic == response_topic
                    and payload.get("type") == "video"
                    and payload.get("action") == "startCapture"
                ):

                    logger.info(
                        f"Found video message: state={payload.get('state')}, code={payload.get('code')}"
                    )

                    if (
                        payload.get("state") == "initSuccess"
                        and payload.get("code") == 200
                    ):
                        logger.info("Received video capture initialization success")
                        self.video_init_success = True
                        return True
                return False

            # Register this handler in the class
            if not hasattr(self, "message_handlers"):
                self.message_handlers = []
            self.message_handlers.append(video_response_handler)

            # Make sure we're subscribed to the right topic
            self.anycubic_client.subscribe(response_topic)

            # Small delay to ensure subscription is processed
            time.sleep(0.5)

            # Send the request
            logger.info(f"Requesting video capture start with topic: {topic}")
            self.anycubic_client.publish(topic, json.dumps(capture_request))

            # Poll for success with timeout
            start_time = time.time()
            timeout_duration = 10.0  # 10 seconds timeout

            while (
                not self.video_init_success
                and (time.time() - start_time) < timeout_duration
            ):
                time.sleep(0.1)  # Check every 100ms

            # Clean up
            if hasattr(self, "message_handlers"):
                self.message_handlers.remove(video_response_handler)

            # Check if we succeeded or timed out
            if not self.video_init_success:
                logger.warning(
                    "Video capture initialization timed out after 10 seconds"
                )

            return self.video_init_success

        except Exception as e:
            logger.error(f"Error starting video capture: {e}")
            return False

    def take_snapshot(self):
        """Capture a snapshot from the camera stream"""
        # Try HTTP snapshot endpoint first
        try:
            logger.info(f"Taking snapshot from HTTP endpoint: http://{ANYCUBIC_S1_IP}/webcam/?action=snapshot")
            image_data = self._capture_snapshot_http()
            
            if image_data:
                logger.debug("Successfully captured snapshot via HTTP")
                return image_data
            else:
                logger.warning("HTTP snapshot failed, trying fallback method")
        except Exception as e:
            logger.warning(f"HTTP snapshot failed with error: {e}, trying fallback method")
        
        # Fallback to existing stream-based method
        if not self.stream_url:
            logger.warning("No camera stream URL available for fallback snapshot")
            return None

        try:
            # Try to take a snapshot directly without starting video capture
            logger.info(f"Taking fallback snapshot from {self.stream_url}")
            image_data = self._capture_snapshot_with_ffmpeg()

            # If snapshot successful, return it
            if image_data:
                return image_data

            # If snapshot failed, try starting video capture
            logger.info("Direct snapshot failed, attempting to start video capture")
            if self.start_video_capture():
                logger.info("Video capture started, trying snapshot again")
                # Small delay to let video initialization complete
                time.sleep(1)
                # Try again after starting video capture
                return self._capture_snapshot_with_ffmpeg()
            else:
                logger.error(
                    "Failed to start video capture and snapshot attempt failed"
                )
                return None

        except Exception as e:
            logger.error(f"Error taking snapshot: {e}")
            logger.debug(traceback.format_exc())
            return None

    def _capture_snapshot_http(self):
        """Capture a snapshot using the HTTP snapshot endpoint"""
        try:
            # Build the snapshot URL
            snapshot_url = f"http://{ANYCUBIC_S1_IP}/webcam/?action=snapshot"
            
            # Make HTTP request to get the snapshot
            response = requests.get(snapshot_url, timeout=10)
            
            if response.status_code == 200:
                # Check if we got valid image data
                content_type = response.headers.get('content-type', '')
                if 'image' in content_type.lower():
                    logger.debug(f"Successfully captured HTTP snapshot ({len(response.content)} bytes)")
                    return response.content
                else:
                    logger.warning(f"HTTP snapshot returned unexpected content type: {content_type}")
                    return None
            else:
                logger.warning(f"HTTP snapshot failed with status code: {response.status_code}")
                return None
                
        except requests.exceptions.Timeout:
            logger.warning("HTTP snapshot request timed out")
            return None
        except requests.exceptions.ConnectionError:
            logger.warning("HTTP snapshot connection failed")
            return None
        except Exception as e:
            logger.error(f"Error capturing HTTP snapshot: {e}")
            logger.debug(traceback.format_exc())
            return None

    def _capture_snapshot_with_ffmpeg(self):
        """Internal method to capture a snapshot using ffmpeg"""
        try:
            import subprocess
            import tempfile
            import os

            # Check if it's an FLV stream
            if self.stream_url.endswith(".flv") or "/flv" in self.stream_url:
                # Create a temporary file for the snapshot
                with tempfile.NamedTemporaryFile(
                    suffix=".jpg", delete=False
                ) as temp_file:
                    temp_path = temp_file.name

                # Use ffmpeg to capture a single frame from the stream
                ffmpeg_cmd = [
                    "ffmpeg",
                    "-y",  # Overwrite output without asking
                    "-timeout",
                    "5000000",  # 5 second connection timeout in microseconds
                    "-analyzeduration",
                    "1000000",  # Analyze only 1 second of stream
                    "-probesize",
                    "1000000",  # Use small probe size
                    "-i",
                    self.stream_url,
                    "-vframes",
                    "1",  # Extract just one video frame
                    "-q:v",
                    "2",  # High quality
                    temp_path,
                ]

                logger.debug(f"Running ffmpeg command: {' '.join(ffmpeg_cmd)}")
                result = subprocess.run(
                    ffmpeg_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=7,  # Set a reasonable timeout
                )

                if result.returncode == 0:
                    # Read the captured image
                    with open(temp_path, "rb") as img_file:
                        image_data = img_file.read()

                    # Clean up temp file
                    try:
                        os.unlink(temp_path)
                    except Exception as e:
                        logger.error(f"Error cleaning up temp file: {e}")

                    logger.info("Successfully captured frame with ffmpeg")
                    return image_data
                else:
                    logger.warning(f"ffmpeg failed: {result.stderr.decode()}")
                    return None

            # Add support for other stream types if needed
            return None

        except (ImportError, FileNotFoundError) as e:
            if isinstance(e, FileNotFoundError):
                try:
                    cmd0 = ffmpeg_cmd[0] if 'ffmpeg_cmd' in locals() and ffmpeg_cmd else 'ffmpeg'
                except Exception:
                    cmd0 = 'ffmpeg'
                missing = getattr(e, "filename", None) or cmd0
                logger.warning(f"ffmpeg invocation failed: file not found: {missing} ({e})")
                logger.info("Hint: Install FFmpeg and ensure 'ffmpeg' is on your PATH. On Windows, you can run 'winget install Gyan.FFmpeg' or download a build and add its 'bin' folder to PATH, then restart this service.")
            else:
                logger.warning(f"Could not use ffmpeg: {e}")
                logger.info("Hint: Install the FFmpeg package for your OS and ensure the 'ffmpeg' executable is available on PATH.")
            return None
        except Exception as e:
            logger.error(f"Error using ffmpeg: {e}")
            logger.debug(traceback.format_exc())
            return None

    def snapshot_worker(self):
        """Background worker that periodically takes snapshots"""
        # Set initial state based on connection status
        if (
            self.anycubic_connection_state == ConnectionState.CONNECTED
            and self.stream_url
        ):
            self.snapshot_enabled.set()
        else:
            self.snapshot_enabled.clear()

        # Get the topic prefix once
        topic_prefix = self.get_topic_prefix()

        # Track the last snapshot time
        last_snapshot_time = 0

        while running and self.stream_url:
            try:
                current_time = time.time()

                # Determine which interval to use based on printer state
                if hasattr(self, "printer_state") and self.printer_state == "free":
                    current_interval = self.snapshot_interval_idle
                else:
                    current_interval = self.snapshot_interval_busy

                # Skip if snapshots are disabled for this state (interval = 0)
                if current_interval == 0:
                    time.sleep(1)  # Sleep a short time before checking again
                    continue

                # Only process if snapshots are enabled and enough time has passed
                if (
                    not self.snapshot_enabled.is_set()
                    or (current_time - last_snapshot_time) < current_interval
                ):
                    time.sleep(1)
                    continue

                logger.info(
                    f"Taking snapshot (state: {getattr(self, 'printer_state', 'unknown')}, interval: {current_interval}s)"
                )
                image_data = self.take_snapshot()

                if image_data:
                    # Publish snapshot as raw binary data with prefix
                    self.ha_client.publish(
                        f"homeassistant/image/{topic_prefix}_snapshot/image",
                        image_data,
                        retain=True,
                    )

                    # Publish state update (timestamp) with prefix
                    self.ha_client.publish(
                        f"homeassistant/image/{topic_prefix}_snapshot/state",
                        time.strftime("%Y-%m-%d %H:%M:%S"),
                        retain=True,
                    )

                    logger.info(
                        f"Published snapshot to Home Assistant with prefix {topic_prefix}"
                    )
                    last_snapshot_time = current_time
                else:
                    logger.warning("Failed to capture snapshot")
                    # Wait a shorter time before retry on failure
                    time.sleep(min(current_interval / 3, 10))
                    continue
            except Exception as e:
                logger.error(f"Error in snapshot worker: {e}")
                time.sleep(5)  # Wait a bit on error before trying again

            # Sleep a short time to prevent high CPU usage (we'll check again in 1 second)
            time.sleep(1)

    def info_update_worker(self):
        """Background worker that periodically requests printer information"""
        while running:
            try:
                # Only request info if the Anycubic client is connected
                if self.anycubic_client and self.anycubic_client.is_connected():
                    self.request_printer_info()
                else:
                    logger.debug("Skipping printer info request - Anycubic client not connected")
            except Exception as e:
                logger.error(f"Error in info update worker: {e}")

            # Sleep until next update
            time.sleep(self.info_update_interval)

    def _create_light_entity(self):
        """Create a light entity in Home Assistant for printer light control"""
        # Get topic prefix and device info
        topic_prefix = self.get_topic_prefix()
        device_info = self.get_device_info()

        # Light entity configuration with dynamic topic prefix
        light_config = {
            "name": "Printer Light",
            "unique_id": f"{topic_prefix}_light",
            "state_topic": f"homeassistant/light/{topic_prefix}_light/state",
            "command_topic": f"homeassistant/light/{topic_prefix}_light/set",
            "brightness_state_topic": f"homeassistant/light/{topic_prefix}_light/brightness",
            "brightness_command_topic": f"homeassistant/light/{topic_prefix}_light/brightness/set",
            "brightness_scale": 255,
            "on_command_type": "brightness",
            "payload_on": "ON",
            "payload_off": "OFF",
            "optimistic": False,
            "qos": 0,
            "device": device_info,
        }

        # Publish light entity configuration
        self.ha_client.publish(
            f"homeassistant/light/{topic_prefix}_light/config",
            json.dumps(light_config),
            retain=True,
        )
        logger.info(
            f"Created printer light entity in Home Assistant with prefix {topic_prefix}"
        )

        # Subscribe to command topics (with prefix)
        self.ha_client.subscribe(f"homeassistant/light/{topic_prefix}_light/set")
        self.ha_client.subscribe(
            f"homeassistant/light/{topic_prefix}_light/brightness/set"
        )

        # Set up message handlers for Home Assistant commands
        self.ha_client.message_callback_add(
            f"homeassistant/light/{topic_prefix}_light/set", self._on_ha_light_command
        )
        self.ha_client.message_callback_add(
            f"homeassistant/light/{topic_prefix}_light/brightness/set",
            self._on_ha_brightness_command,
        )

        self.light_entity_created = True

    def _on_ha_light_command(self, client, userdata, msg):
        """Handle light on/off commands from Home Assistant"""
        try:
            payload = msg.payload.decode("utf-8")
            logger.info(f"Received light command from Home Assistant: {payload}")

            # Translate ON/OFF to the printer's format
            light_status = 1 if payload == "ON" else 0

            # Send command to printer
            self._set_printer_light(status=light_status)
        except Exception as e:
            logger.error(f"Error processing light command: {e}")

    def _on_ha_brightness_command(self, client, userdata, msg):
        """Handle brightness commands from Home Assistant"""
        try:
            brightness_ha = int(msg.payload.decode("utf-8"))
            logger.info(
                f"Received brightness command from Home Assistant: {brightness_ha}"
            )

            # Convert from Home Assistant's 0-255 range to printer's 0-100 range
            brightness = min(100, max(0, int((brightness_ha * 100) / 255)))

            # Send command to printer with updated brightness
            self._set_printer_light(status=1, brightness=brightness)
        except Exception as e:
            logger.error(f"Error processing brightness command: {e}")

    def _set_printer_light(self, status, brightness=None):
        """Send light command to printer"""
        if not self.printer_mode_id or not self.printer_device_id:
            logger.warning("Cannot control light: Missing printer mode ID or device ID")
            return False

        try:
            import uuid

            # Create message ID
            message_id = str(uuid.uuid4())

            # Format the topic
            topic = f"anycubic/anycubicCloud/v1/web/printer/{self.printer_mode_id}/{self.printer_device_id}/light"

            # Get the last known brightness if not specified
            if brightness is None:
                brightness = getattr(self, "light_brightness", 100)
            else:
                # Save the brightness for future use
                self.light_brightness = brightness

            # Create request payload
            light_request = {
                "type": "light",
                "action": "control",
                "timestamp": int(time.time() * 1000),
                "msgid": message_id,
                "data": {"type": 2, "status": status, "brightness": brightness},
            }

            # Send the request
            logger.info(
                f"Setting printer light: status={status}, brightness={brightness}"
            )
            self.anycubic_client.publish(topic, json.dumps(light_request))
            return True

        except Exception as e:
            logger.error(f"Error controlling printer light: {e}")
            return False
                
    def on_anycubic_message(self, client, userdata, msg):
        """Process messages from Anycubic and republish to Home Assistant"""
        try:
            payload = msg.payload.decode("utf-8")
            logger.debug(f"Received from Anycubic: {msg.topic}")

            try:
                data = json.loads(payload)

                message_type = str(data.get("type") or "").strip().lower()
                error_report = classify_mqtt_error(data)
                if message_type in {"print", "error", "info"} or error_report["print_problem"]:
                    self._publish_mqtt_error_state(data)

                # Check for registered message handlers
                if hasattr(self, "message_handlers"):
                    for handler in self.message_handlers:
                        if handler(msg.topic, data):
                            return  # Message was handled, stop processing

                # If this is a light report message
                if "type" in data and data["type"] == "light":
                    try:
                        # First, check if this is a response to a light control command
                        if (
                            data.get("action") == "control"
                            and data.get("state") == "done"
                        ):
                            logger.info(
                                f"Light control command completed with code: {data.get('code')}"
                            )
                            # Refresh light status to get current state
                            self._query_light_status()
                            return

                        # Extract light data with better error checking
                        if (
                            "data" in data
                            and data["data"] is not None
                            and "lights" in data["data"]
                            and data["data"]["lights"] is not None
                            and len(data["data"]["lights"]) > 0
                        ):
                            light_data = data["data"]["lights"][0]
                            light_status = light_data.get("status", 0)
                            light_brightness = light_data.get("brightness", 0)
                            light_type = light_data.get("type", 0)

                            logger.info(
                                f"Received light status: on={light_status==1}, brightness={light_brightness}, type={light_type}"
                            )

                            # Create light entity if it doesn't exist yet
                            if (
                                not hasattr(self, "light_entity_created")
                                or not self.light_entity_created
                            ):
                                self._create_light_entity()

                            # Get topic prefix for publishing
                            topic_prefix = self.get_topic_prefix()

                            # Publish state to Home Assistant
                            self.ha_client.publish(
                                f"homeassistant/light/{topic_prefix}_light/state",
                                "ON" if light_status == 1 else "OFF",
                                retain=True,
                            )

                            # Publish brightness level (0-255 for Home Assistant)
                            brightness = int(
                                min(255, max(0, (light_brightness * 255) / 100))
                            )
                            self.ha_client.publish(
                                f"homeassistant/light/{topic_prefix}_light/brightness",
                                str(brightness),
                                retain=True,
                            )

                            return  # Successfully handled
                        else:
                            logger.debug(
                                f"Light message did not contain expected data structure: {json.dumps(data)}"
                            )
                    except Exception as e:
                        logger.error(f"Error processing light data: {e}")
                        import traceback

                        logger.debug(traceback.format_exc())

                # If this is an info report message with printer status
                if "type" in data and data["type"] == "info":
                    printer_data = data["data"]
                    logger.debug(f"Received printer info: {json.dumps(printer_data)}")
                    topic_prefix = self.get_topic_prefix()
                    self.device_name = printer_data.get(
                        "printerName", "Anycubic Printer"
                    )
                    self.device_model = printer_data.get("model") or getattr(
                        self, "printer_model", None
                    ) or "Unknown"
                    firmware_version = get_firmware_version(
                        printer_data,
                        (
                            "version",
                            "fw_version",
                            "firmware_version",
                            "firmwareVersion",
                            "fwVersion",
                            "firmware",
                        ),
                    )
                    if firmware_version:
                        self.printer_version = firmware_version

                    # Define the device for all sensors
                    device_info = self.get_device_info()

                    # Update device info with any additional discovered properties
                    if firmware_version:
                        device_info["sw_version"] = firmware_version

                    # Create individual sensors
                    sensors = []

                    # State sensor
                    if "state" in printer_data:
                        sensors.append(
                            {
                                "name": "Printer State",
                                "unique_id": f"{topic_prefix}_printer_state",
                                "state_topic": f"homeassistant/sensor/{topic_prefix}/state",
                                "value_template": "{{ value_json.state }}",
                                "icon": "mdi:printer-3d",
                                "device": device_info,
                            }
                        )

                    # Temperature sensors
                    if "temp" in printer_data:
                        # Current hotbed temperature
                        sensors.append(
                            {
                                "name": "Hotbed Temperature",
                                "unique_id": f"{topic_prefix}_hotbed_temp",
                                "state_topic": f"homeassistant/sensor/{topic_prefix}/temperature",
                                "value_template": "{{ value_json.hotbed_temp }}",
                                "unit_of_measurement": "°C",
                                "device_class": "temperature",
                                "icon": "mdi:printer-3d-nozzle-heat",
                                "device": device_info,
                            }
                        )

                        # Current nozzle temperature
                        sensors.append(
                            {
                                "name": "Nozzle Temperature",
                                "unique_id": f"{topic_prefix}_nozzle_temp",
                                "state_topic": f"homeassistant/sensor/{topic_prefix}/temperature",
                                "value_template": "{{ value_json.nozzle_temp }}",
                                "unit_of_measurement": "°C",
                                "device_class": "temperature",
                                "icon": "mdi:printer-3d-nozzle-heat",
                                "device": device_info,
                            }
                        )

                    # Print speed mode
                    if "print_speed_mode" in printer_data:
                        sensors.append(
                            {
                                "name": "Print Speed Mode",
                                "unique_id": f"{topic_prefix}_print_speed_mode",
                                "state_topic": f"homeassistant/sensor/{topic_prefix}/state",
                                "value_template": "{{ value_json.print_speed_mode_name }}",
                                "icon": "mdi:speedometer",
                                "device": device_info,
                            }
                        )

                    # IP Address sensor
                    if "ip" in printer_data:
                        sensors.append(
                            {
                                "name": "Printer IP Address",
                                "unique_id": f"{topic_prefix}_printer_ip",
                                "state_topic": f"homeassistant/sensor/{topic_prefix}/state",
                                "value_template": "{{ value_json.ip_address }}",
                                "icon": "mdi:ip-network",
                                "entity_category": "diagnostic",
                                "device": device_info,
                            }
                        )

                    # Store the stream URL for snapshot use if available
                    if "urls" in printer_data and "rtspUrl" in printer_data["urls"]:
                        self.stream_url = printer_data["urls"]["rtspUrl"]

                        # Create a simple image entity for the snapshot
                        topic_prefix = self.get_topic_prefix()
                        image_config = {
                            "name": "Anycubic Snapshot",
                            "unique_id": f"{topic_prefix}_snapshot",
                            "state_topic": f"homeassistant/image/{topic_prefix}_snapshot/state",
                            "image_topic": f"homeassistant/image/{topic_prefix}_snapshot/image",
                            "content_type": "image/jpeg",
                            "device": device_info,
                        }

                        # Publish image entity config
                        self.ha_client.publish(
                            f"homeassistant/image/{topic_prefix}_snapshot/config",
                            json.dumps(image_config),
                            retain=True,
                        )
                        logger.debug(
                            f"Published snapshot image entity config with prefix {topic_prefix}"
                        )

                        # Start snapshot thread if not already running
                        if (
                            self.snapshot_thread is None
                            or not self.snapshot_thread.is_alive()
                        ):
                            self.snapshot_thread = Thread(target=self.snapshot_worker)
                            self.snapshot_thread.daemon = True
                            self.snapshot_thread.start()
                            logger.info(
                                f"Started snapshot thread with interval {self.snapshot_interval}s"
                            )

                        if OBICO_ENABLED:
                            self._create_obico_entities()
                            self._start_obico_worker()

                    # Publish all sensor configurations
                    for sensor in sensors:
                        discovery_topic = (
                            f"homeassistant/sensor/{sensor['unique_id']}/config"
                        )
                        self.ha_client.publish(
                            discovery_topic, json.dumps(sensor), retain=True
                        )

                    # Create the combined state message
                    topic_prefix = self.get_topic_prefix()

                    # Keep the last known state when an info report omits it.
                    reported_state = printer_data.get("state")
                    self.printer_state = resolve_printer_state(
                        reported_state,
                        self.current_print_state,
                        self.printer_state,
                    )
                    logger.debug(f"Updated printer state: {self.printer_state}")

                    # Also publish a separate fan update if fan data is available
                    if any(
                        key in printer_data
                        for key in (
                            "fan_speed_pct",
                            "aux_fan_speed_pct",
                            "box_fan_level",
                        )
                    ):
                        fan_state = {
                            "fan_speed_pct": printer_data.get("fan_speed_pct", 0),
                            "aux_fan_speed_pct": printer_data.get("aux_fan_speed_pct", 0),
                            "box_fan_level": printer_data.get("box_fan_level", 0) if "box_fan_level" in printer_data else 0,
                            "last_updated": time.strftime("%Y-%m-%d %H:%M:%S"),
                        }
                        self.latest_fan_state = dict(fan_state)
                        
                        # Publish fan data to Home Assistant
                        self.ha_client.publish(
                            f"homeassistant/sensor/{topic_prefix}/fan",
                            json.dumps(fan_state),
                            retain=True
                        )
                        
                        logger.debug(f"Published fan update from info report: {json.dumps(fan_state)}")

                    # Also publish a separate temperature update if temperature data is available
                    if "temp" in printer_data:
                        temp_state = {
                            "hotbed_temp": printer_data.get("temp", {}).get("curr_hotbed_temp", 0),
                            "nozzle_temp": printer_data.get("temp", {}).get("curr_nozzle_temp", 0),
                            "target_hotbed_temp": printer_data.get("temp", {}).get("target_hotbed_temp", 0),
                            "target_nozzle_temp": printer_data.get("temp", {}).get("target_nozzle_temp", 0),
                            "last_updated": time.strftime("%Y-%m-%d %H:%M:%S"),
                        }
                        self.latest_temperature_state = dict(temp_state)
                        
                        # Publish temperature data to Home Assistant
                        self.ha_client.publish(
                            f"homeassistant/sensor/{topic_prefix}/temperature",
                            json.dumps(temp_state),
                            retain=True
                        )
                        
                        logger.debug(f"Published temperature update from info report: {json.dumps(temp_state)}")


                    # Also extract and publish print job data if available
                    if "project" in printer_data and printer_data["project"]:
                        project_data = printer_data["project"]
                        logger.debug(f"Extracted print job data from info report: {json.dumps(project_data)}")
                        
                        # Save the print data for future use
                        if not hasattr(self, "latest_print_data"):
                            self.latest_print_data = {}
                        
                        # Merge project reports because some omit progress fields.
                        self.latest_print_data = merge_print_data(
                            self.latest_print_data,
                            project_data,
                            self.current_print_state,
                        )
                        
                        # Create/update print sensors if not already created
                        if not hasattr(self, "print_sensors_created") or not self.print_sensors_created:
                            self._create_print_sensors()
                        
                        # Create print job state update
                        print_state = build_print_job_state(
                            self.latest_print_data, self.current_print_state
                        )
                        print_state["last_updated"] = time.strftime(
                            "%Y-%m-%d %H:%M:%S"
                        )
                        
                        # Publish print job data to Home Assistant
                        self.ha_client.publish(
                            f"homeassistant/sensor/{topic_prefix}/print_job",
                            json.dumps(print_state),
                            retain=True
                        )
                        
                        logger.debug(f"Published print job update from info report: {json.dumps(print_state)}")
                        
                        # Create print controls if needed
                        if not hasattr(self, "print_controls_created") or not self.print_controls_created:
                            self.print_controls_created = self._create_print_controls()

                    state_data = {
                        "state": self.printer_state,
                        "print_speed_mode": printer_data.get("print_speed_mode", 0),
                        "print_speed_mode_name": print_speed_mode_name(
                            printer_data.get("print_speed_mode")
                        ),
                        "ip_address": printer_data.get("ip", ""),
                        "camera_url": printer_data.get("urls", {}).get("rtspUrl", ""),
                        "printer_cn": self.printer_cn if self.printer_cn else "",
                        "last_updated": time.strftime("%Y-%m-%d %H:%M:%S"),
                    }
                    self.latest_printer_state = dict(state_data)

                    # Publish the state to the CN-specific topic
                    self.ha_client.publish(
                        f"homeassistant/sensor/{topic_prefix}/state",
                        json.dumps(state_data),
                        retain=True,
                    )
                    logger.info("Published printer state data to Home Assistant")
                                # If this is a temperature report message
                elif "type" in data and data["type"] == "tempature":
                    logger.info("Received temperature report")
                    try:
                        if "data" in data and data["data"]:
                            temp_data = data["data"]
                            
                            # Save the temperature data for future use
                            if not hasattr(self, "latest_temp_data"):
                                self.latest_temp_data = {}
                            
                            # Update our stored temperature data
                            self.latest_temp_data.update(temp_data)
                            
                            # Get topic prefix for publishing
                            topic_prefix = self.get_topic_prefix()
                            
                            # Create a state update with just temperature information
                            temp_state = {
                                "hotbed_temp": temp_data.get("curr_hotbed_temp", 0),
                                "nozzle_temp": temp_data.get("curr_nozzle_temp", 0),
                                "target_hotbed_temp": temp_data.get("target_hotbed_temp", 0),
                                "target_nozzle_temp": temp_data.get("target_nozzle_temp", 0),
                                "last_updated": time.strftime("%Y-%m-%d %H:%M:%S"),
                            }
                            self.latest_temperature_state = dict(temp_state)
                            
                            # Publish temperature data to Home Assistant
                            self.ha_client.publish(
                                f"homeassistant/sensor/{topic_prefix}/temperature",
                                json.dumps(temp_state),
                                retain=True
                            )
                            
                            logger.debug(f"Published temperature update: {json.dumps(temp_state)}")
                    except Exception as e:
                        logger.error(f"Error processing temperature data: {e}")
                        logger.debug(traceback.format_exc())

                elif "type" in data and data["type"] == "print":
                    logger.info(f"Received print job report: action={data.get('action', 'unknown')}, state={data.get('state', 'unknown')}")
                    logger.debug(f"Print job report data: {json.dumps(data)}")
                    try:
                        previous_print_state = self.current_print_state
                        self.current_print_state = resolve_print_job_state(
                            data.get("action"),
                            data.get("state"),
                            self.current_print_state,
                        )
                        if self.current_print_state == "printing":
                            self.printer_state = "busy"
                        elif self.current_print_state == "paused":
                            self.printer_state = "paused"
                        elif self.current_print_state == "free":
                            self.printer_state = "free"
                        elif self.current_print_state in {"leveling", "preheating"}:
                            self.printer_state = self.current_print_state

                        topic_prefix = self.get_topic_prefix()
                        state_data = dict(getattr(self, "latest_printer_state", {}))
                        state_data["state"] = self.printer_state
                        state_data["last_updated"] = time.strftime(
                            "%Y-%m-%d %H:%M:%S"
                        )
                        self.latest_printer_state = state_data
                        self.ha_client.publish(
                            f"homeassistant/sensor/{topic_prefix}/state",
                            json.dumps(state_data),
                            retain=True,
                        )

                        if "data" in data and data["data"]:
                            print_data = data["data"]
                            
                            # Save the print data for future use
                            if not hasattr(self, "latest_print_data"):
                                self.latest_print_data = {}
                            
                            # Update our stored print data
                            self.latest_print_data = merge_print_data(
                                self.latest_print_data,
                                print_data,
                                self.current_print_state,
                                reset=(
                                    data.get("action") == "start"
                                    and previous_print_state != "printing"
                                ),
                            )
                            
                            # Create/update print sensors if not already created
                            if not hasattr(self, "print_sensors_created") or not self.print_sensors_created:
                                self._create_print_sensors()
                            
                            # Get topic prefix for publishing
                            topic_prefix = self.get_topic_prefix()
                            
                            # Create a state update with print job information
                            print_state = build_print_job_state(
                                self.latest_print_data, self.current_print_state
                            )
                            print_state["last_updated"] = time.strftime(
                                "%Y-%m-%d %H:%M:%S"
                            )
                            
                            # Publish print job data to Home Assistant
                            self.ha_client.publish(
                                f"homeassistant/sensor/{topic_prefix}/print_job",
                                json.dumps(print_state),
                                retain=True
                            )
                            
                            logger.debug(f"Published print job update: {json.dumps(print_state)}")
                            
                            # If we receive a completion message, clear the print job data
                            if data.get("action") in {
                                "stop",
                                "cancel",
                                "finish",
                                "complete",
                            }:
                                logger.info("Print job completed")
                            
                            # Create print controls
                            if not hasattr(self, "print_controls_created") or not self.print_controls_created:
                                self.print_controls_created = self._create_print_controls()

                    except Exception as e:
                        logger.error(f"Error processing print job data: {e}")
                        logger.debug(traceback.format_exc())
                
                elif "type" in data and data["type"] == "multiColorBox":
                    logger.info("Received multicolor box report")
                    # log the full report at debug level
                    logger.debug(f"Multicolor box report data: {json.dumps(data)}")
                    try:
                        if "data" in data and "multi_color_box" in data["data"] and data["data"]["multi_color_box"]:
                            # Get topic prefix for publishing
                            topic_prefix = self.get_topic_prefix()
                            
                            box_items = data["data"]["multi_color_box"]
                            identified_boxes = [
                                (get_color_box_id(box_data, fallback_index), box_data)
                                for fallback_index, box_data in enumerate(box_items)
                            ]
                            box_ids = {box_id for box_id, _ in identified_boxes}
                            boxes_to_refresh = set()
                            for box_id, color_box_data in identified_boxes:
                                latest_box_data = self.latest_color_box_data.setdefault(
                                    box_id, {}
                                )
                                latest_box_data.update(color_box_data)
                                firmware = get_firmware_version(
                                    latest_box_data,
                                    (
                                        "firmware",
                                        "firmware_version",
                                        "firmwareVersion",
                                        "fw_version",
                                        "fwVersion",
                                        "version",
                                    ),
                                )
                                if box_id not in self.discovered_color_box_ids or (
                                    firmware
                                    and self.discovered_color_box_firmware.get(box_id)
                                    != firmware
                                ):
                                    boxes_to_refresh.add(box_id)
                                if firmware:
                                    self.discovered_color_box_firmware[box_id] = firmware

                            if boxes_to_refresh:
                                logger.info(
                                    "Publishing ACE Pro device info for box IDs: %s",
                                    sorted(boxes_to_refresh),
                                )
                                self._create_color_box_sensors(boxes_to_refresh)
                                self.discovered_color_box_ids.update(boxes_to_refresh)

                            for box_id, color_box_data in identified_boxes:
                                latest_box_data = self.latest_color_box_data[box_id]
                                box_state = {
                                    "box_id": box_id,
                                    "status": latest_box_data.get("status", 0),
                                    "temperature": latest_box_data.get("temp", 0),
                                    "loaded_slot": latest_box_data.get("loaded_slot", -1),
                                    "auto_feed": latest_box_data.get("auto_feed", 0),
                                    "last_updated": time.strftime("%Y-%m-%d %H:%M:%S"),
                                }
                                self.ha_client.publish(
                                    f"homeassistant/sensor/{topic_prefix}/color_box_{box_id}",
                                    json.dumps(box_state),
                                    retain=True,
                                )
                                if box_id == 0:
                                    self.ha_client.publish(
                                        f"homeassistant/sensor/{topic_prefix}/color_box",
                                        json.dumps(box_state),
                                        retain=True,
                                    )

                                for slot_position, slot in enumerate(
                                    latest_box_data.get("slots", [])
                                ):
                                    local_slot = normalize_box_slot_index(
                                        slot.get("index", slot_position), box_id
                                    )
                                    if local_slot is None:
                                        logger.debug(
                                            "Ignoring invalid slot index %s for ACE Pro box %s",
                                            slot.get("index"),
                                            box_id,
                                        )
                                        continue
                                    global_slot = global_filament_index(
                                        box_id, local_slot
                                    )
                                    slot_data = {
                                        "box_id": box_id,
                                        "box_slot_index": local_slot,
                                        "slot_index": global_slot,
                                        "type": slot.get("type", "Unknown"),
                                        "status": slot.get("status", 0),
                                        "color_rgb": slot.get("color", [128, 128, 128]),
                                        "last_updated": time.strftime("%Y-%m-%d %H:%M:%S"),
                                    }
                                    rgb = slot.get("color")
                                    if isinstance(rgb, list) and len(rgb) == 3:
                                        slot_data["color_hex"] = f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"
                                    self.ha_client.publish(
                                        f"homeassistant/sensor/{topic_prefix}/filament_slot_{global_slot}",
                                        json.dumps(slot_data),
                                        retain=True,
                                    )

                                logger.debug(
                                    "Published multicolor box %s update with %d slots",
                                    box_id,
                                    len(latest_box_data.get("slots", [])),
                                )
                    except Exception as e:
                        logger.error(f"Error processing multicolor box data: {e}")
                        logger.debug(traceback.format_exc())

                elif "type" in data and data["type"] == "fan":
                    logger.info("Received fan speed report")
                    try:
                        if "data" in data and data["data"]:
                            fan_data = data["data"]
                            
                            # Save the fan data for future use
                            if not hasattr(self, "latest_fan_data"):
                                self.latest_fan_data = {}
                            
                            # Update our stored fan data
                            self.latest_fan_data.update(fan_data)
                            
                            # Get topic prefix for publishing
                            topic_prefix = self.get_topic_prefix()
                            
                            # Create a state update with just fan information
                            fan_state = {
                                "fan_speed_pct": fan_data.get("fan_speed_pct", 0),
                                "aux_fan_speed_pct": fan_data.get("aux_fan_speed_pct", 0),
                                "box_fan_level": fan_data.get("box_fan_level", 0),
                                "last_updated": time.strftime("%Y-%m-%d %H:%M:%S"),
                            }
                            self.latest_fan_state = dict(fan_state)
                            
                            # Publish fan data to Home Assistant
                            self.ha_client.publish(
                                f"homeassistant/sensor/{topic_prefix}/fan",
                                json.dumps(fan_state),
                                retain=True
                            )
                            
                            logger.debug(f"Published fan update: {json.dumps(fan_state)}")
                    except Exception as e:
                        logger.error(f"Error processing fan data: {e}")
                        logger.debug(traceback.format_exc())

                elif "type" in data and data["type"] == "file":
                    logger.info(f"Received file report: action={data.get('action', 'unknown')}")
                    logger.debug(f"File report data: {json.dumps(data)}")
                    try:
                        if "data" in data and data["data"] and "file_details" in data["data"] and "thumbnail" in data["data"]["file_details"]:
                            # Extract the base64 thumbnail
                            thumbnail_base64 = data["data"]["file_details"]["thumbnail"]
                            filename = data["data"].get("filename", "Unknown File")
                            
                            try:
                                # Decode base64 to binary image data
                                import base64
                                thumbnail_binary = base64.b64decode(thumbnail_base64)
                                
                                # Create the image entity if it doesn't exist
                                if not hasattr(self, "print_preview_created") or not self.print_preview_created:
                                    self._create_print_preview_entity()
                                
                                # Get topic prefix
                                topic_prefix = self.get_topic_prefix()
                                
                                # Publish the thumbnail image
                                self.ha_client.publish(
                                    f"homeassistant/image/{topic_prefix}_print_preview/image",
                                    thumbnail_binary,
                                    retain=True
                                )
                                
                                # Publish state (current filename)
                                self.ha_client.publish(
                                    f"homeassistant/image/{topic_prefix}_print_preview/state",
                                    filename,
                                    retain=True
                                )
                                
                                logger.info(f"Published print preview image for file: {filename}")
                            except Exception as e:
                                logger.error(f"Error decoding thumbnail: {e}")
                                logger.debug(traceback.format_exc())
                    
                    except Exception as e:
                        logger.error(f"Error processing file data: {e}")
                        logger.debug(traceback.format_exc())
                        
                else:
                    # Handle other types of messages
                    topic_parts = msg.topic.split("/")
                    if len(topic_parts) > 2:
                        topic_type = topic_parts[-2]
                        topic_action = topic_parts[-1]

                        # Check if this is a system message with a hash-like ID (silently handle)
                        is_system_message = len(topic_type) == 32 and all(
                            c in "0123456789abcdef" for c in topic_type.lower()
                        )

                        if not is_system_message:
                            logger.info(
                                f"Unhandled message type: {topic_type}/{topic_action}"
                            )
                            logger.info(f"Message data: {json.dumps(data, indent=2)}")
                        else:
                            # Log at debug level only
                            logger.debug(f"System message: {topic_type}/{topic_action}")

            except json.JSONDecodeError:
                logger.warning(f"Received non-JSON data on topic {msg.topic}")

        except Exception as e:
            logger.error(f"Error processing message: {e}")
            import traceback

            logger.error(traceback.format_exc())

    def _on_pause_print(self, client, userdata, msg):
        """Handle pause print button press from Home Assistant"""
        try:
            logger.info("Received pause print command from Home Assistant")
            self._send_print_command("pause")
        except Exception as e:
            logger.error(f"Error processing pause print command: {e}")
    
    def _on_resume_print(self, client, userdata, msg):
        """Handle resume print button press from Home Assistant"""
        try:
            logger.info("Received resume print command from Home Assistant")
            self._send_print_command("resume")
        except Exception as e:
            logger.error(f"Error processing resume print command: {e}")
    
    def _on_cancel_print(self, client, userdata, msg):
        """Handle cancel print button press from Home Assistant"""
        try:
            logger.info("Received cancel print command from Home Assistant")
            self._send_print_command("stop")
        except Exception as e:
            logger.error(f"Error processing cancel print command: {e}")
    
    def _send_print_command(self, action):
        """Send print control command to the printer"""
        if not self.printer_mode_id or not self.printer_device_id:
            logger.error("Cannot send print command: Missing printer device ID or mode ID")
            return False

        try:
            import uuid

            # Create message ID
            message_id = str(uuid.uuid4())

            # Format the topic
            topic = f"anycubic/anycubicCloud/v1/web/printer/{self.printer_mode_id}/{self.printer_device_id}/print"

            # Create request payload
            print_request = {
                "type": "print",
                "action": action,
                "timestamp": int(time.time() * 1000),
                "msgid": message_id,
                "data": None,
            }

            # Send the request
            logger.info(f"Sending print {action} command to printer")
            self.anycubic_client.publish(topic, json.dumps(print_request))
            return True

        except Exception as e:
            logger.error(f"Error sending print command: {e}")
            logger.debug(traceback.format_exc())
            return False

    def _create_color_box_sensors(self, box_ids):
        """Create Home Assistant entities for detected physical ACE Pro boxes."""
        # Get topic prefix and device info
        topic_prefix = self.get_topic_prefix()
        
        for box_index in sorted(set(box_ids)):
            box_number = box_index
            firmware = get_firmware_version(
                getattr(self, "latest_color_box_data", {}).get(box_index, {}),
                (
                    "firmware",
                    "firmware_version",
                    "firmwareVersion",
                    "fw_version",
                    "fwVersion",
                    "version",
                ),
            )
            device_info = self.get_color_box_device_info(box_index, firmware)
            # List of box sensors to create for this specific box
            sensors = [
                {
                    "name": f"ACE Pro Box {box_number} Status",
                    "unique_id": f"{topic_prefix}_color_box_{box_index}_status",
                    "state_topic": f"homeassistant/sensor/{topic_prefix}/color_box_{box_index}",
                    "value_template": "{{ 'Connected' if value_json.status == 1 else 'Disconnected' }}",
                    "icon": "mdi:printer-3d-nozzle",
                    "device": device_info,
                },
                {
                    "name": f"ACE Pro Box {box_number} Temperature",
                    "unique_id": f"{topic_prefix}_color_box_{box_index}_temp",
                    "state_topic": f"homeassistant/sensor/{topic_prefix}/color_box_{box_index}",
                    "value_template": "{{ value_json.temperature }}",
                    "unit_of_measurement": "°C",
                    "device_class": "temperature",
                    "icon": "mdi:thermometer",
                    "device": device_info,
                },
                {
                    "name": f"ACE Pro Box {box_number} Active Slot",
                    "unique_id": f"{topic_prefix}_color_box_{box_index}_active_filament_slot",
                    "state_topic": f"homeassistant/sensor/{topic_prefix}/color_box_{box_index}",
                    "value_template": "{{ value_json.loaded_slot if value_json.loaded_slot >= 0 else 'None' }}",
                    "icon": "mdi:numeric",
                    "device": device_info,
                },
            ]
            
            for local_slot in range(4):
                global_slot = global_filament_index(box_index, local_slot)
                slot_topic = f"homeassistant/sensor/{topic_prefix}/filament_slot_{global_slot}"
                sensors.extend(
                    (
                        {
                            "name": f"Filament {global_slot} Type",
                            "unique_id": f"{topic_prefix}_filament_{global_slot}_type",
                            "state_topic": slot_topic,
                            "value_template": "{{ value_json.type }}",
                            "icon": "mdi:spool",
                            "device": device_info,
                        },
                        {
                            "name": f"Filament {global_slot} Color",
                            "unique_id": f"{topic_prefix}_filament_{global_slot}_color",
                            "state_topic": slot_topic,
                            "value_template": "{{ value_json.color_hex }}",
                            "icon": "mdi:palette",
                            "device": device_info,
                        },
                    )
                )
            
            # Publish all sensor configurations for this box
            for sensor in sensors:
                discovery_topic = f"homeassistant/sensor/{sensor['unique_id']}/config"
                self.ha_client.publish(discovery_topic, json.dumps(sensor), retain=True)
        
        logger.info("Created ACE Pro sensors for box IDs: %s", sorted(set(box_ids)))

    def _create_print_controls(self):
        """Create buttons in Home Assistant to control print jobs"""
        if not self.ha_client or not self.ha_client.is_connected():
            logger.debug("Deferring print controls until Home Assistant MQTT connects")
            return False

        # Get topic prefix and device info
        topic_prefix = self.get_topic_prefix()
        device_info = self.get_device_info()
        
        # List of control buttons to create
        buttons = [
            {
                "name": "Pause Print",
                "unique_id": f"{topic_prefix}_pause_print",
                "command_topic": f"homeassistant/button/{topic_prefix}_pause_print/command",
                "payload_press": "PRESS",
                "icon": "mdi:pause",
                "device": device_info,
            },
            {
                "name": "Resume Print",
                "unique_id": f"{topic_prefix}_resume_print",
                "command_topic": f"homeassistant/button/{topic_prefix}_resume_print/command",
                "payload_press": "PRESS",
                "icon": "mdi:play",
                "device": device_info,
            },
            {
                "name": "Cancel Print",
                "unique_id": f"{topic_prefix}_cancel_print",
                "command_topic": f"homeassistant/button/{topic_prefix}_cancel_print/command",
                "payload_press": "PRESS",
                "icon": "mdi:stop",
                "device": device_info,
            }
        ]
        
        # Publish all button configurations
        for button in buttons:
            discovery_topic = f"homeassistant/button/{button['unique_id']}/config"
            self.ha_client.publish(discovery_topic, json.dumps(button), retain=True)
        
        # Subscribe to command topics
        self.ha_client.subscribe(f"homeassistant/button/{topic_prefix}_pause_print/command")
        self.ha_client.subscribe(f"homeassistant/button/{topic_prefix}_resume_print/command")
        self.ha_client.subscribe(f"homeassistant/button/{topic_prefix}_cancel_print/command")
        
        # Set up message handlers
        self.ha_client.message_callback_add(
            f"homeassistant/button/{topic_prefix}_pause_print/command", 
            self._on_pause_print
        )
        self.ha_client.message_callback_add(
            f"homeassistant/button/{topic_prefix}_resume_print/command", 
            self._on_resume_print
        )
        self.ha_client.message_callback_add(
            f"homeassistant/button/{topic_prefix}_cancel_print/command", 
            self._on_cancel_print
        )
        
        logger.info("Created print control buttons in Home Assistant")
        self._create_temperature_fan_controls()
        self._create_print_error_sensors()
        return True

    def _create_print_error_sensors(self):
        if getattr(self, "print_error_sensors_created", False):
            return

        topic_prefix = self.get_topic_prefix()
        state_topic = f"homeassistant/sensor/{topic_prefix}/print_error"
        device_info = self.get_device_info()
        sensors = (
            {
                "name": "Job Failed",
                "unique_id": f"{topic_prefix}_job_failed",
                "value_template": "{{ 'ON' if value_json.job_failed else 'OFF' }}",
                "payload_on": "ON",
                "payload_off": "OFF",
                "device_class": "problem",
                "json_attributes_topic": state_topic,
                "state_topic": state_topic,
                "icon": "mdi:printer-3d-alert",
                "device": device_info,
            },
            {
                "name": "Print Problem",
                "unique_id": f"{topic_prefix}_print_problem",
                "value_template": "{{ 'ON' if value_json.print_problem else 'OFF' }}",
                "payload_on": "ON",
                "payload_off": "OFF",
                "device_class": "problem",
                "json_attributes_topic": state_topic,
                "state_topic": state_topic,
                "icon": "mdi:alert-circle-outline",
                "device": device_info,
            },
        )
        for sensor in sensors:
            self.ha_client.publish(
                f"homeassistant/binary_sensor/{sensor['unique_id']}/config",
                json.dumps(sensor),
                retain=True,
            )

        last_error_config = {
            "name": "Last Print Error",
            "unique_id": f"{topic_prefix}_last_print_error",
            "state_topic": f"homeassistant/sensor/{topic_prefix}/last_print_error",
            "value_template": "{{ value_json.message or value_json.state or value_json.code }}",
            "json_attributes_topic": f"homeassistant/sensor/{topic_prefix}/last_print_error",
            "icon": "mdi:message-alert-outline",
            "entity_category": "diagnostic",
            "device": device_info,
        }
        self.ha_client.publish(
            f"homeassistant/sensor/{last_error_config['unique_id']}/config",
            json.dumps(last_error_config),
            retain=True,
        )
        self.print_error_sensors_created = True

    def _publish_mqtt_error_state(self, message):
        self._create_print_error_sensors()
        error_state = classify_mqtt_error(message)
        error_state.update(
            {"type": message.get("type"), "action": message.get("action")}
        )
        original_message = str(error_state.get("message") or "")
        error_state["original_message"] = original_message
        error_state["message"] = translate_mqtt_error_message(original_message)
        error_state.update(
            {
                "last_updated": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
        self.ha_client.publish(
            f"homeassistant/sensor/{self.get_topic_prefix()}/print_error",
            json.dumps(error_state),
            retain=True,
        )
        if error_state["print_problem"]:
            self.ha_client.publish(
                f"homeassistant/sensor/{self.get_topic_prefix()}/last_print_error",
                json.dumps(error_state),
                retain=True,
            )
            logger.warning(
                "Printer MQTT problem: type=%s action=%s state=%s code=%s message=%s",
                error_state["type"],
                error_state["action"],
                error_state["state"],
                error_state["code"],
                error_state["message"],
            )

    def _create_temperature_fan_controls(self):
        if getattr(self, "temperature_fan_controls_created", False):
            return

        topic_prefix = self.get_topic_prefix()
        device_info = self.get_device_info()
        temperature_state_topic = f"homeassistant/sensor/{topic_prefix}/temperature"
        fan_state_topic = f"homeassistant/sensor/{topic_prefix}/fan"
        speed_state_topic = f"homeassistant/sensor/{topic_prefix}/state"
        speed_command_topic = f"homeassistant/select/{topic_prefix}_print_speed_mode_control/set"
        speed_select = {
            "name": "Print Speed Mode Control",
            "unique_id": f"{topic_prefix}_print_speed_mode_control",
            "state_topic": speed_state_topic,
            "value_template": "{{ value_json.print_speed_mode_name }}",
            "command_topic": speed_command_topic,
            "options": list(PRINT_SPEED_MODE_NAMES.values()),
            "availability": [
                {
                    "topic": speed_state_topic,
                    "value_template": (
                        "{{ 'online' if value_json.state in "
                        "['busy', 'printing', 'paused', 'pausing', 'resuming', 'preheating'] "
                        "else 'offline' }}"
                    ),
                }
            ],
            "icon": "mdi:speedometer",
            "device": device_info,
        }
        self.ha_client.publish(
            f"homeassistant/select/{speed_select['unique_id']}/config",
            json.dumps(speed_select),
            retain=True,
        )
        self.ha_client.message_callback_add(
            speed_command_topic, self._on_print_speed_mode_command
        )
        self.ha_client.subscribe(speed_command_topic)

        temperature_controls = (
            {
                "name": "Target Nozzle Temperature",
                "key": "target_nozzle_temp",
                "maximum": 320,
                "icon": "mdi:printer-3d-nozzle",
                "handler": self._on_nozzle_temperature_command,
            },
            {
                "name": "Target Hotbed Temperature",
                "key": "target_hotbed_temp",
                "maximum": 120,
                "icon": "mdi:heating-coil",
                "handler": self._on_hotbed_temperature_command,
            },
        )
        for control in temperature_controls:
            unique_id = f"{topic_prefix}_{control['key']}_control"
            command_topic = f"homeassistant/number/{unique_id}/set"
            config = {
                "name": control["name"],
                "unique_id": unique_id,
                "state_topic": temperature_state_topic,
                "value_template": f"{{{{ value_json.{control['key']} }}}}",
                "command_topic": command_topic,
                "min": 0,
                "max": control["maximum"],
                "step": 1,
                "mode": "slider",
                "unit_of_measurement": "°C",
                "device_class": "temperature",
                "icon": control["icon"],
                "device": device_info,
            }
            self.ha_client.publish(
                f"homeassistant/number/{unique_id}/config",
                json.dumps(config),
                retain=True,
            )
            self.ha_client.message_callback_add(command_topic, control["handler"])
            self.ha_client.subscribe(command_topic)

        fan_controls = (
            ("Main Fan", "fan_speed_pct", device_info, "mdi:fan"),
            ("Aux Fan", "aux_fan_speed_pct", device_info, "mdi:fan"),
            (
                "Box Fan",
                "box_fan_level",
                device_info,
                "mdi:fan",
            ),
        )
        for name, key, fan_device_info, icon in fan_controls:
            unique_id = f"{topic_prefix}_{key}_control"
            command_topic = f"homeassistant/fan/{unique_id}/percentage/set"
            config = {
                "name": name,
                "unique_id": unique_id,
                "state_topic": fan_state_topic,
                "state_value_template": (
                    f"{{{{ 'ON' if value_json.{key} | int(0) > 0 else 'OFF' }}}}"
                ),
                "percentage_state_topic": fan_state_topic,
                "percentage_value_template": f"{{{{ value_json.{key} }}}}",
                "percentage_command_topic": command_topic,
                "command_topic": command_topic,
                "payload_on": "ON",
                "payload_off": "OFF",
                "speed_range_min": 1,
                "speed_range_max": 100,
                "icon": icon,
                "device": fan_device_info,
            }
            self.ha_client.publish(
                f"homeassistant/fan/{unique_id}/config",
                json.dumps(config),
                retain=True,
            )
            self.ha_client.message_callback_add(
                command_topic,
                lambda client, userdata, msg, fan_key=key: self._on_fan_command(
                    client, userdata, msg, fan_key
                ),
            )
            self.ha_client.subscribe(command_topic)

        self.temperature_fan_controls_created = True
        logger.info("Created temperature and fan controls in Home Assistant")

    def _on_print_speed_mode_command(self, client, userdata, msg):
        option = msg.payload.decode("utf-8").strip()
        mode = next(
            (mode for mode, name in PRINT_SPEED_MODE_NAMES.items() if name == option),
            None,
        )
        if mode is None:
            logger.warning("Ignoring invalid print speed mode: %s", option)
            return

        active_states = {
            "busy",
            "printing",
            "paused",
            "pausing",
            "resuming",
            "preheating",
        }
        if not any(
            str(state or "").strip().lower() in active_states
            for state in (
                getattr(self, "printer_state", None),
                getattr(self, "current_print_state", None),
            )
        ):
            logger.info("Ignoring print speed mode change while no print is active")
            return

        print_data = getattr(self, "latest_print_data", {})
        task_id = next(
            (
                str(print_data[key])
                for key in ("taskid", "project_id", "id")
                if print_data.get(key) is not None
            ),
            "-1",
        )
        if not self._publish_anycubic_command(
            "print",
            "print",
            "update",
            {"taskid": task_id, "settings": {"print_speed_mode": mode}},
        ):
            return

        state = dict(getattr(self, "latest_printer_state", {}))
        state["print_speed_mode"] = mode
        state["print_speed_mode_name"] = PRINT_SPEED_MODE_NAMES[mode]
        state["last_updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self.latest_printer_state = state
        self.ha_client.publish(
            f"homeassistant/sensor/{self.get_topic_prefix()}/state",
            json.dumps(state),
            retain=True,
        )

    def _publish_anycubic_command(self, endpoint, message_type, action, data):
        if not self.printer_mode_id or not self.printer_device_id:
            logger.error("Cannot send %s command: Missing printer IDs", endpoint)
            return False

        import uuid

        payload = {
            "type": message_type,
            "action": action,
            "timestamp": int(time.time() * 1000),
            "msgid": str(uuid.uuid4()),
            "data": data,
        }
        root = "anycubic/anycubicCloud/v1"
        topics = (
            f"{root}/printer/public/{self.printer_mode_id}/{self.printer_device_id}/{endpoint}",
            f"{root}/pc/printer/{self.printer_mode_id}/{self.printer_device_id}/{endpoint}",
        )
        try:
            for topic in topics:
                self.anycubic_client.publish(topic, json.dumps(payload))
            return True
        except Exception as e:
            logger.error("Error sending %s command: %s", endpoint, e)
            return False

    def _on_nozzle_temperature_command(self, client, userdata, msg):
        self._on_temperature_command("target_nozzle_temp", 320, msg)

    def _on_hotbed_temperature_command(self, client, userdata, msg):
        self._on_temperature_command("target_hotbed_temp", 120, msg)

    def _on_temperature_command(self, key, maximum, msg):
        try:
            requested = float(msg.payload.decode("utf-8").strip())
            if not requested.is_integer():
                raise ValueError("temperature must be a whole number")
            value = int(requested)
        except (UnicodeDecodeError, ValueError):
            logger.warning("Ignoring invalid temperature command: %r", msg.payload)
            return

        if not 0 <= value <= maximum:
            logger.warning("Ignoring out-of-range %s: %s", key, value)
            return

        settings = {}
        current_state = getattr(self, "latest_temperature_state", {})
        if value != 0:
            for target_key in ("target_nozzle_temp", "target_hotbed_temp"):
                current_value = current_state.get(target_key)
                if current_value is not None:
                    settings[target_key] = int(current_value)
        settings[key] = value

        if not self._publish_anycubic_command(
            "tempature", "tempature", "set", settings
        ):
            return

        temperature_state = dict(current_state)
        temperature_state.setdefault("hotbed_temp", None)
        temperature_state.setdefault("nozzle_temp", None)
        temperature_state["target_nozzle_temp"] = settings.get(
            "target_nozzle_temp", current_state.get("target_nozzle_temp")
        )
        temperature_state["target_hotbed_temp"] = settings.get(
            "target_hotbed_temp", current_state.get("target_hotbed_temp")
        )
        temperature_state[key] = value
        temperature_state["last_updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self.latest_temperature_state = temperature_state
        self.ha_client.publish(
            f"homeassistant/sensor/{self.get_topic_prefix()}/temperature",
            json.dumps(temperature_state),
            retain=True,
        )

    def _on_fan_command(self, client, userdata, msg, fan_key):
        try:
            payload = msg.payload.decode("utf-8").strip().upper()
            if payload == "ON":
                value = 100
            elif payload == "OFF":
                value = 0
            else:
                requested = float(payload)
                if not requested.is_integer():
                    raise ValueError("fan speed must be a whole number")
                value = int(requested)
        except (UnicodeDecodeError, ValueError):
            logger.warning("Ignoring invalid fan command: %r", msg.payload)
            return

        if not 0 <= value <= 100:
            logger.warning("Ignoring out-of-range fan value: %s", value)
            return

        task_id = "-1"
        print_data = getattr(self, "latest_print_data", {})
        for task_key in ("taskid", "project_id", "id"):
            if print_data.get(task_key) is not None:
                task_id = str(print_data[task_key])
                break

        if fan_key == "fan_speed_pct":
            fan_data = {
                key: int(current_value)
                for key, current_value in getattr(self, "latest_fan_state", {}).items()
                if key in {"fan_speed_pct", "aux_fan_speed_pct", "box_fan_level"}
                and current_value is not None
            }
            fan_data["fan_speed_pct"] = value
            fan_data["taskid"] = task_id
            for action in ("setSpeed", "auto"):
                if not self._publish_anycubic_command(
                    "fan", "fan", action, fan_data
                ):
                    return
        else:
            if not self._publish_anycubic_command(
                "print",
                "print",
                "update",
                {"taskid": task_id, "settings": {fan_key: value}},
            ):
                return
            if not self._publish_anycubic_command(
                "fan", "fan", "auto", {fan_key: value}
            ):
                return

        fan_state = dict(getattr(self, "latest_fan_state", {}))
        fan_state[fan_key] = value
        fan_state["last_updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self.latest_fan_state = fan_state
        self.ha_client.publish(
            f"homeassistant/sensor/{self.get_topic_prefix()}/fan",
            json.dumps(fan_state),
            retain=True,
        )

    def _create_print_preview_entity(self):
        """Create a print preview image entity in Home Assistant"""
        # Get topic prefix and device info
        topic_prefix = self.get_topic_prefix()
        device_info = self.get_device_info()
        
        # Create image entity configuration
        image_config = {
            "name": "Print Preview",
            "unique_id": f"{topic_prefix}_print_preview",
            "state_topic": f"homeassistant/image/{topic_prefix}_print_preview/state",
            "image_topic": f"homeassistant/image/{topic_prefix}_print_preview/image",
            "content_type": "image/png",
            "device": device_info,
        }
        
        # Publish entity configuration
        self.ha_client.publish(
            f"homeassistant/image/{topic_prefix}_print_preview/config",
            json.dumps(image_config),
            retain=True
        )
        
        # Mark entity as created
        self.print_preview_created = True
        logger.info("Created print preview image entity in Home Assistant")

    def _create_print_sensors(self):
        """Create print job related sensors in Home Assistant"""
        # Get topic prefix and device info
        topic_prefix = self.get_topic_prefix()
        device_info = self.get_device_info()
        
        # List of print sensors to create
        sensors = [
            {
                "name": "Print Filename",
                "unique_id": f"{topic_prefix}_print_filename",
                "state_topic": f"homeassistant/sensor/{topic_prefix}/print_job",
                "value_template": "{{ value_json.filename }}",
                "icon": "mdi:file-document",
                "device": device_info,
            },
            {
                "name": "Print Progress",
                "unique_id": f"{topic_prefix}_print_progress",
                "state_topic": f"homeassistant/sensor/{topic_prefix}/print_job",
                "value_template": "{{ value_json.progress }}",
                "unit_of_measurement": "%",
                "icon": "mdi:progress-check",
                "device": device_info,
            },
            {
                "name": "Print Layer",
                "unique_id": f"{topic_prefix}_print_layer",
                "state_topic": f"homeassistant/sensor/{topic_prefix}/print_job",
                "value_template": "{{ value_json.current_layer }} / {{ value_json.total_layers }}",
                "icon": "mdi:layers",
                "device": device_info,
            },
            {
                "name": "Print Time Elapsed",
                "unique_id": f"{topic_prefix}_print_time",
                "state_topic": f"homeassistant/sensor/{topic_prefix}/print_job",
                "value_template": "{{ value_json.print_time }}",
                "unit_of_measurement": "min",
                "icon": "mdi:clock-time-four",
                "device": device_info,
            },
            {
                "name": "Print Time Remaining",
                "unique_id": f"{topic_prefix}_print_remaining",
                "state_topic": f"homeassistant/sensor/{topic_prefix}/print_job",
                "value_template": "{{ value_json.remaining_time }}",
                "unit_of_measurement": "min",
                "icon": "mdi:clock-time-eight",
                "device": device_info,
            },
            {
                "name": "Material Usage",
                "unique_id": f"{topic_prefix}_material_usage",
                "state_topic": f"homeassistant/sensor/{topic_prefix}/print_job",
                "value_template": "{{ value_json.material_usage }}",
                "unit_of_measurement": "mm",
                "icon": "mdi:printer-3d-nozzle",
                "device": device_info,
            },
            {
                "name": "Print State",
                "unique_id": f"{topic_prefix}_print_state",
                "state_topic": f"homeassistant/sensor/{topic_prefix}/print_job",
                "value_template": "{{ value_json.state }}",
                "icon": "mdi:printer-3d",
                "device": device_info,
            },
        ]
        
        # Publish all sensor configurations
        for sensor in sensors:
            discovery_topic = f"homeassistant/sensor/{sensor['unique_id']}/config"
            self.ha_client.publish(discovery_topic, json.dumps(sensor), retain=True)
        
        # Mark sensors as created
        self.print_sensors_created = True
        logger.info("Created print job sensors in Home Assistant")

    def generate_sign(self, token, ts, nonce):
        """
        Generate the sign parameter using the algorithm:
        sign = md5(md5(token.slice(0, 16)) + ts + nonce)
        """
        import hashlib
        import urllib.parse

        # Take first 16 characters of token
        token_part = token[:16]

        # First MD5 hash
        first_md5 = hashlib.md5(token_part.encode()).hexdigest()

        # Concatenate with timestamp and nonce
        combined = first_md5 + str(ts) + nonce

        # Second MD5 hash
        second_md5 = hashlib.md5(combined.encode()).hexdigest()

        # Double URL encode (encodeURIComponent followed by encodeURI)
        # In Python this is approximately equivalent to:
        encoded = urllib.parse.quote(urllib.parse.quote(second_md5, safe=""))

        return encoded

    def decrypt_printer_data(self, encrypted_data, token, local_token):
        """
        Decrypt the printer data using AES-CBC with PKCS7 padding
        - encrypted_data: The encrypted data from the response
        - token: The token from HTTP discovery, we need slice(16, 32)
        - local_token: The token from the response
        """
        try:
            # Try multiple import paths for compatibility with different installations
            try:
                from Crypto.Cipher import AES  # type: ignore
                from Crypto.Util.Padding import unpad  # type: ignore
            except ImportError:
                # Try alternative path used by some Linux distributions
                try:
                    from Cryptodome.Cipher import AES  # type: ignore
                    from Cryptodome.Util.Padding import unpad  # type: ignore

                    logger.info("Using Cryptodome instead of Crypto")
                except ImportError:
                    raise ImportError("Cannot find PyCryptodome or PyCryptodomex")

            import base64

            # Extract the key from token (second half)
            key = token[16:32].encode("utf-8")

            # Use the local token as IV
            iv = local_token.encode("utf-8")

            # Ensure IV is 16 bytes (pad or truncate)
            if len(iv) < 16:
                iv = iv + (b"\0" * (16 - len(iv)))
            else:
                iv[:16]

            # Decode the base64 encoded data
            encrypted_bytes = base64.b64decode(encrypted_data)

            # Create the cipher
            cipher = AES.new(key, AES.MODE_CBC, iv)

            # Decrypt and unpad
            decrypted_bytes = unpad(cipher.decrypt(encrypted_bytes), AES.block_size)

            # Convert to string and parse JSON
            decrypted_text = decrypted_bytes.decode("utf-8")
            return json.loads(decrypted_text)

        except ImportError:
            logger.error(
                "Cannot decrypt printer data: PyCryptodome library not installed"
            )
            logger.info("Install with: pip install pycryptodome")
            return None

        except Exception as e:
            logger.error(f"Error decrypting printer data: {e}")
            logger.debug(traceback.format_exc())
            return None

    def discover_printer(self):
        """Discover the Anycubic printer on the network using HTTP"""
        # Declare globals at the beginning of the function
        global ANYCUBIC_S1_IP, ANYCUBIC_BROKER, ANYCUBIC_PORT, ANYCUBIC_USER, ANYCUBIC_PASS

        try:
            logger.info(f"Attempting to discover Anycubic printer at {ANYCUBIC_S1_IP}")

            # Step 1: Get basic printer info
            try:
                logger.info("Attempting HTTP discovery")
                info_url = f"http://{ANYCUBIC_S1_IP}:18910/info"
                response = requests.get(info_url, timeout=5)

                if response.status_code == 200:
                    data = response.json()
                    logger.info(f"Received HTTP discovery response: {data}")
                    firmware_version = get_firmware_version(
                        data,
                        (
                            "version",
                            "fw_version",
                            "firmware_version",
                            "firmwareVersion",
                            "fwVersion",
                            "firmware",
                        ),
                    )
                    if firmware_version:
                        self.printer_version = firmware_version

                    # Check if printer is in cloud mode
                    if data.get("ctrlType") == "cloud":
                        logger.warning(
                            "Printer is set to CLOUD mode. Please set the printer to LAN mode for local control."
                        )

                    # Store basic printer info
                    if "modelId" in data:
                        self.printer_mode_id = data["modelId"]
                    if "modelName" in data:
                        self.printer_model = data["modelName"]

                    # Step 2: Get detailed printer info by calling the control URL
                    if "ctrlInfoUrl" in data and "token" in data and "cn" in data:
                        ctrl_url = data["ctrlInfoUrl"]
                        token = data["token"]
                        self.printer_cn = data["cn"]

                        # Current timestamp in milliseconds
                        ts = int(time.time() * 1000)

                        # Generate a random nonce (6 characters)
                        import random
                        import string

                        nonce = "".join(
                            random.choices(string.ascii_letters + string.digits, k=6)
                        )

                        # Generate PC DID
                        pc_did = "".join(
                            random.choices(string.ascii_uppercase + string.digits, k=32)
                        )

                        # Generate sign parameter using the correct algorithm
                        sign = self.generate_sign(token, ts, nonce)

                        # Make the control request with all required parameters
                        logger.info(f"Making control request to {ctrl_url}")

                        ctrl_params = {
                            "ts": ts,
                            "nonce": nonce,
                            "sign": sign,
                            "did": pc_did,
                        }

                        # Make the POST request
                        ctrl_response = requests.post(
                            ctrl_url, params=ctrl_params, timeout=5
                        )

                        if ctrl_response.status_code == 200:
                            ctrl_data = ctrl_response.json()
                            logger.info(
                                f"Received control response with status code: {ctrl_data.get('code')}"
                            )

                            # Check if we got a success response
                            if (
                                ctrl_data.get("code") == 200
                                and ctrl_data.get("message") == "success"
                            ):
                                # Process the response data
                                if "data" in ctrl_data and "info" in ctrl_data["data"]:
                                    # Store the token if available
                                    if "token" in ctrl_data["data"]:
                                        local_token = ctrl_data["data"]["token"]
                                        logger.info(
                                            f"Received printer token: {local_token}"
                                        )

                                        # Get the encrypted printer data
                                        printer_encrypted_data = ctrl_data["data"][
                                            "info"
                                        ]
                                        logger.info(
                                            "Received encrypted printer data, decrypting..."
                                        )

                                        # Decrypt the data
                                        printer_data = self.decrypt_printer_data(
                                            printer_encrypted_data,
                                            token,  # Original token from HTTP request
                                            local_token,
                                        )

                                        if printer_data:
                                            logger.info(
                                                "Successfully decrypted printer data"
                                            )
                                            firmware_version = get_firmware_version(
                                                printer_data,
                                                (
                                                    "version",
                                                    "fw_version",
                                                    "firmware_version",
                                                    "firmwareVersion",
                                                    "fwVersion",
                                                    "firmware",
                                                ),
                                            )
                                            if firmware_version:
                                                self.printer_version = firmware_version

                                            # Extract connection parameters from decrypted data
                                            if "broker" in printer_data:
                                                broker_url = printer_data["broker"]
                                                # Parse the broker URL (format: mqtts://192.168.86.59:9883)
                                                import re

                                                broker_match = re.match(
                                                    r"mqtts?://([^:]+):(\d+)",
                                                    broker_url,
                                                )
                                                if broker_match:
                                                    # Update the global broker settings
                                                    ANYCUBIC_BROKER = (
                                                        broker_match.group(1)
                                                    )
                                                    ANYCUBIC_PORT = int(
                                                        broker_match.group(2)
                                                    )
                                                    logger.info(
                                                        f"Updated broker settings: {ANYCUBIC_BROKER}:{ANYCUBIC_PORT}"
                                                    )

                                            # Extract authentication info
                                            if "username" in printer_data:
                                                ANYCUBIC_USER = printer_data["username"]
                                                logger.info(
                                                    f"Using discovered username: {ANYCUBIC_USER}"
                                                )

                                            if "password" in printer_data:
                                                ANYCUBIC_PASS = printer_data["password"]
                                                logger.info("Using discovered password")

                                            # Extract device ID, mode ID, and CN
                                            if "deviceId" in printer_data:
                                                self.printer_device_id = printer_data[
                                                    "deviceId"
                                                ]
                                                logger.info(
                                                    f"Using discovered device ID: {self.printer_device_id}"
                                                )

                                            if "modeId" in printer_data:
                                                self.printer_mode_id = printer_data[
                                                    "modeId"
                                                ]
                                                logger.info(
                                                    f"Using discovered mode ID: {self.printer_mode_id}"
                                                )

                                            if "modelName" in printer_data:
                                                self.printer_model = printer_data[
                                                    "modelName"
                                                ]
                                                logger.info(
                                                    f"Using discovered model name: {self.printer_model}"
                                                )

                                            # Also save the certificate and private key if needed for future use
                                            if (
                                                "devicecrt" in printer_data
                                                and "devicepk" in printer_data
                                            ):
                                                logger.info(
                                                    "Discovered device certificate and private key"
                                                )

                                            # Return success instead of calling exit()
                                            return True
                            else:
                                logger.warning(
                                    f"Control request returned error: {ctrl_data}"
                                )
                        else:
                            logger.warning(
                                f"Control request failed with status code: {ctrl_response.status_code}"
                            )

                    # If we have the necessary information, consider discovery successful
                    if self.printer_mode_id and self.printer_device_id:
                        logger.info(
                            f"Discovery successful: Model={self.printer_model}, ModeID={self.printer_mode_id}, DeviceID={self.printer_device_id}"
                        )
                        return True
                    else:
                        logger.warning(
                            "Discovery incomplete - missing mode ID or device ID"
                        )
                        return False
                else:
                    logger.warning(
                        f"HTTP discovery request failed with status code: {response.status_code}"
                    )
                    return False

            except Exception as http_error:
                logger.warning(f"HTTP discovery failed: {http_error}")
                logger.debug(traceback.format_exc())
                return False

        except Exception as e:
            logger.error(f"Error during printer discovery: {e}")
            logger.debug(traceback.format_exc())
            return False


def signal_handler(sig, frame):
    """Handle termination signals"""
    global running
    logger.info("Shutdown signal received, exiting...")
    running = False


def main():
    """Main function to run the bridge"""
    logger.info(f"Anycubic MQTT Bridge v{VERSION} starting up...")
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Save terminal settings (skip on Windows)
    old_settings = None
    if sys.platform != 'win32':
        try:
            import termios
            fd = sys.stdin.fileno()
            old_settings = termios.tcgetattr(fd)
        except (ImportError, termios.error):
            old_settings = None

    try:
        bridge = AnycubicMqttBridge()
        if not bridge.connect():
            logger.error("Failed to establish initial connections, exiting")
            return 1

        logger.info("Anycubic MQTT bridge is running")

        # Keep the main thread running
        while running:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        bridge.disconnect()
        logger.info("Bridge stopped")

        # Restore terminal settings (skip on Windows)
        if old_settings and sys.platform != 'win32':
            try:
                import termios
                fd = sys.stdin.fileno()
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
            except Exception:
                pass

    return 0

if __name__ == "__main__":
    sys.exit(main())
