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
from threading import Thread, Event
import traceback
import random

# Set up logging
logging.basicConfig(
    level=logging.DEBUG, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("anycubic_mqtt_bridge")

# Load configuration from .env file if present
load_dotenv()


# Function to get required environment variables
def get_required_env(name, default=None, required=False):
    value = os.getenv(name, default)
    if required and (value is None or value == default):
        logger.error(f"Required environment variable {name} is not set")
        raise ValueError(f"Required environment variable {name} is not set")
    return value


# Anycubic S1 IP Address - required
ANYCUBIC_S1_IP = get_required_env("ANYCUBIC_S1_IP", required=True)

# Home Assistant MQTT Broker - these are required
HA_BROKER = get_required_env("HA_BROKER", required=True)
HA_PORT = int(get_required_env("HA_PORT", "1883"))
HA_USER = get_required_env("HA_USER")  # Optional
HA_PASS = get_required_env("HA_PASS")  # Optional

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

        self.setup_clients()

    def get_device_info(self):
        """Return consistent device info dictionary for Home Assistant entities"""
        device_id = self.printer_cn if self.printer_cn else "anycubic_printer"
        return {
            "identifiers": [device_id],
            "name": getattr(self, "device_name", "Anycubic Printer"),
            "model": getattr(self, "device_model", "Unknown"),
            "manufacturer": "Anycubic",
            "sw_version": getattr(self, "printer_version", "Unknown"),
        }

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
        if HA_USER and HA_PASS:
            self.ha_client.username_pw_set(HA_USER, HA_PASS)
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
                    f"Connecting to Home Assistant broker at {HA_BROKER}:{HA_PORT}"
                )
                self.ha_client.connect(HA_BROKER, HA_PORT, keepalive=KEEPALIVE_INTERVAL)
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

        # Create a message ID

        message_id = str(uuid.uuid4())

        # Create request payload

        info_request = {
            "type": "info",
            "action": "query",
            "timestamp": int(time.time() * 1000),  # Current time in milliseconds
            "msgid": message_id,
            "data": None,
        }

        # Use the discovered values for the topic

        topic = f"anycubic/anycubicCloud/v1/web/printer/{self.printer_mode_id}/{self.printer_device_id}/info"

        logger.info(
            f"Requesting printer information with topic: {topic}, message ID: {message_id}"
        )

        self.anycubic_client.publish(topic, json.dumps(info_request))

        # Create a message ID

        message_id_light = str(uuid.uuid4())

        # Create request payload

        info_request_light = {
            "type": "light",
            "action": "query",
            "timestamp": int(time.time() * 1000),  # Current time in milliseconds
            "msgid": message_id_light,
            "data": None,
        }

        topic_light = f"anycubic/anycubicCloud/v1/web/printer/{self.printer_mode_id}/{self.printer_device_id}/light"

        logger.info(
            f"Requesting printer light information with topic: {topic_light}, message ID: {message_id_light}"
        )

        self.anycubic_client.publish(topic_light, json.dumps(info_request_light))

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
        else:
            logger.error(
                f"Failed to connect to Home Assistant broker, return code: {rc}"
            )
            self.ha_connection_state = ConnectionState.ERROR
            self.update_connectivity_status()

    def on_ha_disconnect(self, client, userdata, rc):
        """Handle disconnection from Home Assistant broker"""
        logger.warning(f"Disconnected from Home Assistant broker with code {rc}")
        self.ha_connection_state = ConnectionState.DISCONNECTED
        self.update_connectivity_status()
        self.connection_healthy.clear()

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
        if not self.stream_url:
            logger.warning("No camera stream URL available for snapshot")
            return None

        try:
            # First try to take a snapshot directly without starting video capture
            logger.info(f"Taking snapshot from {self.stream_url}")
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
            logger.warning(f"Could not use ffmpeg: {e}")
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
                self.request_printer_info()
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
                    self.device_name = printer_data.get(
                        "printerName", "Anycubic Printer"
                    )
                    self.device_model = printer_data.get("model", "Unknown")

                    # Define the device for all sensors
                    device_info = self.get_device_info()

                    # Update device info with any additional discovered properties
                    if "version" in printer_data:
                        self.printer_version = printer_data["version"]
                        device_info["sw_version"] = printer_data["version"]

                    if "printerName" in printer_data:
                        device_info["name"] = printer_data["printerName"]

                    # Create individual sensors
                    sensors = []

                    # Add Serial Number (CN) sensor
                    if self.printer_cn:
                        # Use prefix for consistency
                        topic_prefix = self.get_topic_prefix()
                        sensors.append(
                            {
                                "name": "Printer Serial Number",
                                "unique_id": f"{topic_prefix}_serial_number",
                                "state_topic": f"homeassistant/sensor/{topic_prefix}/state",
                                "value_template": "{{ value_json.printer_cn }}",
                                "icon": "mdi:barcode",
                                "device": device_info,
                            }
                        )

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
                                "state_topic": f"homeassistant/sensor/{topic_prefix}/state",
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
                                "state_topic": f"homeassistant/sensor/{topic_prefix}/state",
                                "value_template": "{{ value_json.nozzle_temp }}",
                                "unit_of_measurement": "°C",
                                "device_class": "temperature",
                                "icon": "mdi:printer-3d-nozzle-heat",
                                "device": device_info,
                            }
                        )

                    # Fan speed sensors
                    if "fan_speed_pct" in printer_data:
                        sensors.append(
                            {
                                "name": "Fan Speed",
                                "unique_id": f"{topic_prefix}_fan_speed",
                                "state_topic": f"homeassistant/sensor/{topic_prefix}/state",
                                "value_template": "{{ value_json.fan_speed_pct }}",
                                "unit_of_measurement": "%",
                                "icon": "mdi:fan",
                                "device": device_info,
                            }
                        )

                    if "aux_fan_speed_pct" in printer_data:
                        sensors.append(
                            {
                                "name": "Aux Fan Speed",
                                "unique_id": f"{topic_prefix}_aux_fan_speed",
                                "state_topic": f"homeassistant/sensor/{topic_prefix}/state",
                                "value_template": "{{ value_json.aux_fan_speed_pct }}",
                                "unit_of_measurement": "%",
                                "icon": "mdi:fan",
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
                                "value_template": "{{ value_json.print_speed_mode }}",
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
                                "device": device_info,
                            }
                        )

                    # Store the stream URL for snapshot use if available
                    if "urls" in printer_data and "rtspUrl" in printer_data["urls"]:
                        self.stream_url = printer_data["urls"]["rtspUrl"]


                        # Add a URL sensor
                        sensors.append(
                            {
                                "name": "Printer Camera URL",
                                "unique_id": f"{topic_prefix}_printer_camera_url",
                                "state_topic": f"homeassistant/sensor/{topic_prefix}/state",
                                "value_template": "{{ value_json.camera_url }}",
                                "icon": "mdi:cctv",
                                "device": device_info,
                            }
                        )

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
                        logger.info(
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

                    # Save the printer state for the snapshot worker
                    if "state" in printer_data:
                        self.printer_state = printer_data["state"]
                        logger.debug(f"Updated printer state: {self.printer_state}")

                    state_data = {
                        "state": printer_data.get("state", "unknown"),
                        "hotbed_temp": printer_data.get("temp", {}).get(
                            "curr_hotbed_temp", 0
                        ),
                        "nozzle_temp": printer_data.get("temp", {}).get(
                            "curr_nozzle_temp", 0
                        ),
                        "target_hotbed_temp": printer_data.get("temp", {}).get(
                            "target_hotbed_temp", 0
                        ),
                        "target_nozzle_temp": printer_data.get("temp", {}).get(
                            "target_nozzle_temp", 0
                        ),
                        "fan_speed_pct": printer_data.get("fan_speed_pct", 0),
                        "aux_fan_speed_pct": printer_data.get("aux_fan_speed_pct", 0),
                        "print_speed_mode": printer_data.get("print_speed_mode", 0),
                        "ip_address": printer_data.get("ip", ""),
                        "camera_url": printer_data.get("urls", {}).get("rtspUrl", ""),
                        "printer_cn": self.printer_cn if self.printer_cn else "",
                        "last_updated": time.strftime("%Y-%m-%d %H:%M:%S"),
                    }

                    # Publish the state to the CN-specific topic
                    self.ha_client.publish(
                        f"homeassistant/sensor/{topic_prefix}/state",
                        json.dumps(state_data),
                        retain=True,
                    )
                    logger.info("Published printer state data to Home Assistant")
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
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Save terminal settings
    import termios
    import sys

    try:
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
    except termios.error:
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

        # Restore terminal settings
        if old_settings:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
            except Exception:
                pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
