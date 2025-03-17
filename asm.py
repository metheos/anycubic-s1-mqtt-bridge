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
from io import BytesIO
import base64
from threading import Thread
import traceback

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
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

# Global flag for controlling the main loop
running = True

class AnycubicMqttBridge:
    def __init__(self):
        self.anycubic_client = None
        self.ha_client = None
        self.stream_url = None
        self.snapshot_interval = int(get_required_env("SNAPSHOT_INTERVAL", "60"))
        self.info_update_interval = int(get_required_env("INFO_UPDATE_INTERVAL", "30"))
        self.snapshot_thread = None
        self.info_thread = None
        
        # Initialize with empty values - we'll get them from discovery or env vars
        self.printer_mode_id = get_required_env("PRINTER_MODE_ID", None)  # No longer required
        self.printer_device_id = get_required_env("PRINTER_DEVICE_ID", None)  # No longer required
        self.printer_model = get_required_env("PRINTER_MODEL", None)  # No longer required
        
        # Try to discover the printer first
        discovery_successful = self.discover_printer()
        
        # If discovery failed and we don't have all required parameters, we can't proceed
        if not discovery_successful and (not self.printer_mode_id or not self.printer_device_id):
            logger.error("Printer discovery failed and required printer parameters are not provided in environment variables")
            raise ValueError("Cannot establish connection: Missing printer information")

        logger.info(f"Using printer configuration: Model={self.printer_model}, ModeID={self.printer_mode_id}, DeviceID={self.printer_device_id}")
        
        self.setup_clients()

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
        """Connect to both brokers"""
        # Validate that we have the required parameters
        if not self.printer_device_id or not self.printer_mode_id:
            logger.error("Cannot connect: Missing printer device ID or mode ID")
            return False
        
        try:
            logger.info(f"Connecting to Home Assistant broker at {HA_BROKER}:{HA_PORT}")
            self.ha_client.connect(HA_BROKER, HA_PORT, keepalive=KEEPALIVE_INTERVAL)
            self.ha_client.loop_start()
            
            logger.info(f"Connecting to Anycubic broker at {ANYCUBIC_BROKER}:{ANYCUBIC_PORT}")
            logger.info(f"Using printer device ID: {self.printer_device_id}")
            self.anycubic_client.connect(ANYCUBIC_BROKER, ANYCUBIC_PORT, keepalive=KEEPALIVE_INTERVAL)
            self.anycubic_client.loop_start()
            
            # Start the info update thread
            self.info_thread = Thread(target=self.info_update_worker)
            self.info_thread.daemon = True
            self.info_thread.start()
            logger.info(f"Started info update thread with interval {self.info_update_interval}s")
            
            return True
        except Exception as e:
            logger.error(f"Failed to connect: {e}")
            return False

    def disconnect(self):
        """Disconnect from both brokers"""
        logger.info("Disconnecting from brokers")
        if self.ha_client:
            self.ha_client.loop_stop()
            self.ha_client.disconnect()
        if self.anycubic_client:
            self.anycubic_client.loop_stop()
            self.anycubic_client.disconnect()

    def on_anycubic_connect(self, client, userdata, flags, rc):
        """Handle connection to Anycubic broker"""
        if rc == 0:
            logger.info("Connected to Anycubic MQTT broker")
            
            # Subscribe to printer and light
            client.subscribe(f"anycubic/anycubicCloud/v1/printer/+/{self.printer_mode_id}/{self.printer_device_id}/#")
            logger.info("Subscribed to Anycubic printer topics")

            # Subscribe to camera
            client.subscribe(f"anycubic/anycubicCloud/v1/+/public/{self.printer_mode_id}/{self.printer_device_id}/+/report")
            
            # Request printer information after connecting
            self.request_printer_info()
            
            # Also request light status
            self._query_light_status()
        else:
            logger.error(f"Failed to connect to Anycubic broker, return code: {rc}")

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
                "data": None
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
            "data": None
        }
        
        # Use the discovered values for the topic
        topic = f"anycubic/anycubicCloud/v1/web/printer/{self.printer_mode_id}/{self.printer_device_id}/info"
        
        logger.info(f"Requesting printer information with topic: {topic}, message ID: {message_id}")
        self.anycubic_client.publish(topic, json.dumps(info_request))

        
        
        # Create a message ID
        message_id_light = str(uuid.uuid4())
        
        # Create request payload
        info_request_light = {
            "type": "light",
            "action": "query",
            "timestamp": int(time.time() * 1000),  # Current time in milliseconds
            "msgid": message_id_light,
            "data": None
        }

        topic_light = f"anycubic/anycubicCloud/v1/web/printer/{self.printer_mode_id}/{self.printer_device_id}/light"
        
        logger.info(f"Requesting printer light information with topic: {topic_light}, message ID: {message_id_light}")
        self.anycubic_client.publish(topic_light, json.dumps(info_request_light))

    def on_anycubic_disconnect(self, client, userdata, rc):
        """Handle disconnection from Anycubic broker"""
        logger.warning(f"Disconnected from Anycubic broker with code {rc}")
        if rc != 0 and running:
            logger.info("Attempting to reconnect to Anycubic broker in 5 seconds...")
            time.sleep(5)
            try:
                client.reconnect()
            except Exception as e:
                logger.error(f"Reconnect failed: {e}")

    def on_ha_connect(self, client, userdata, flags, rc):
        """Handle connection to Home Assistant broker"""
        if rc == 0:
            logger.info("Connected to Home Assistant MQTT broker")
        else:
            logger.error(f"Failed to connect to Home Assistant broker, return code: {rc}")

    def on_ha_disconnect(self, client, userdata, rc):
        """Handle disconnection from Home Assistant broker"""
        logger.warning(f"Disconnected from Home Assistant broker with code {rc}")
        if rc != 0 and running:
            logger.info("Attempting to reconnect to Home Assistant broker in 5 seconds...")
            time.sleep(5)
            try:
                client.reconnect()
            except Exception as e:
                logger.error(f"Reconnect failed: {e}")
    def start_video_capture(self):
        """Send command to start video capture on the printer"""
        if not self.printer_mode_id or not self.printer_device_id:
            logger.warning("Cannot start video capture: Missing printer mode ID or device ID")
            return False
            
        try:
            import uuid
            from threading import Event, Timer
            
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
                "data": None
            }
            
            # Create a class attribute to track initialization success
            self.video_init_success = False
            self.video_init_message_id = message_id
            
            # Register a callback in the main handler
            def video_response_handler(topic, payload):
                print(f"Received response: {topic} -> {payload}")
                if (topic == response_topic and
                    payload.get("type") == "video" and 
                    payload.get("action") == "startCapture"):
                    
                    logger.info(f"Found video message: state={payload.get('state')}, code={payload.get('code')}")
                    
                    if (payload.get("state") == "initSuccess" and payload.get("code") == 200):
                        logger.info("Received video capture initialization success")
                        self.video_init_success = True
                        return True
                return False
            
            # Register this handler in the class
            if not hasattr(self, 'message_handlers'):
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
            
            while not self.video_init_success and (time.time() - start_time) < timeout_duration:
                time.sleep(0.1)  # Check every 100ms
            
            # Clean up
            if hasattr(self, 'message_handlers'):
                self.message_handlers.remove(video_response_handler)
            
            # Check if we succeeded or timed out
            if not self.video_init_success:
                logger.warning("Video capture initialization timed out after 10 seconds")
            
            return self.video_init_success
            
        except Exception as e:
            logger.error(f"Error starting video capture: {e}")
            return False
        
    def take_snapshot(self):
        """Capture a snapshot from the camera stream"""
        if not self.stream_url:
            logger.warning("No camera stream URL available for snapshot")
            return None
        

        # Start video capture
        self.start_video_capture()

        try:
            logger.info(f"Taking snapshot from {self.stream_url}")
            
            # Check if it's an FLV stream
            if self.stream_url.endswith('.flv') or '/flv' in self.stream_url:
                # Try to use ffmpeg to get a snapshot (if installed)
                try:
                    import subprocess
                    import tempfile
                    
                    # Create a temporary file for the snapshot
                    with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as temp_file:
                        temp_path = temp_file.name
                    
                    # Use ffmpeg to capture a single frame from the stream
                    # -y: Overwrite output files without asking
                    # -i: Input file
                    # -vframes 1: Extract just one video frame
                    # -q:v 2: Set quality (2 is high quality, lower number is better)
                    ffmpeg_cmd = [
                        'ffmpeg',
                        '-y',
                        '-i', self.stream_url,
                        '-vframes', '1',
                        '-q:v', '2',
                        temp_path
                    ]
                    
                    logger.debug(f"Running ffmpeg command: {' '.join(ffmpeg_cmd)}")
                    result = subprocess.run(
                        ffmpeg_cmd, 
                        stdout=subprocess.PIPE, 
                        stderr=subprocess.PIPE,
                        timeout=10  # Set a reasonable timeout
                    )
                    
                    if result.returncode == 0:
                        # Read the captured image
                        with open(temp_path, 'rb') as img_file:
                            image_data = img_file.read()
                        
                        # Clean up temp file
                        try:
                            os.unlink(temp_path)
                        except:
                            pass
                            
                        logger.info("Successfully captured frame with ffmpeg")
                        return image_data
                    else:
                        logger.error(f"ffmpeg failed: {result.stderr.decode()}")
                        
                except (ImportError, FileNotFoundError) as e:
                    logger.warning(f"Could not use ffmpeg: {e}. Falling back to HTTP request.")
                except Exception as e:
                    logger.error(f"Error using ffmpeg: {e}")
                    logger.debug(traceback.format_exc())
            
            # Fallback for other stream types or if ffmpeg fails
            response = requests.get(self.stream_url, stream=True, timeout=5)
            
            # For HTTP stream that returns an image directly
            if 'image' in response.headers.get('Content-Type', ''):
                image_data = response.content
                return image_data
                
            # For MJPEG streams, we need to extract a single frame
            elif 'multipart' in response.headers.get('Content-Type', ''):
                boundary = response.headers.get('Content-Type').split('boundary=')[1]
                for line in response.iter_content(chunk_size=1024):
                    if boundary.encode() in line:
                        # Found the boundary, read the next chunk which should be a JPEG
                        jpeg_data = BytesIO()
                        for chunk in response.iter_content(chunk_size=1024):
                            if boundary.encode() in chunk:
                                break
                            jpeg_data.write(chunk)
                        return jpeg_data.getvalue()
                
            # For other stream types, try a different approach with OpenCV if available
            else:
                try:
                    import cv2
                    
                    # OpenCV can handle many stream types
                    cap = cv2.VideoCapture(self.stream_url)
                    ret, frame = cap.read()
                    cap.release()
                    
                    if ret:
                        # Convert the frame to JPEG
                        ret, buffer = cv2.imencode('.jpg', frame)
                        if ret:
                            return buffer.tobytes()
                except ImportError:
                    logger.warning("OpenCV not available for stream capture")
                    
                # Last resort - try to get an image from the first few KB
                logger.warning("Using fallback download method - may not work")
                try:
                    chunk = next(response.iter_content(chunk_size=100000))
                    return chunk
                except:
                    pass
                    
        except Exception as e:
            logger.error(f"Error taking snapshot: {e}")
            logger.debug(traceback.format_exc())
            return None
    
    def snapshot_worker(self):
        """Background worker that periodically takes snapshots"""
        while running and self.stream_url:
            try:
                image_data = self.take_snapshot()
                
                if image_data:
                    # Publish snapshot as raw binary data
                    self.ha_client.publish(
                        "homeassistant/image/anycubic_printer_snapshot/image",
                        image_data,
                        retain=True
                    )
                    
                    # Publish state update (timestamp)
                    self.ha_client.publish(
                        "homeassistant/image/anycubic_printer_snapshot/state",
                        time.strftime("%Y-%m-%d %H:%M:%S"),
                        retain=True
                    )
                    
                    logger.info("Published snapshot to Home Assistant")
            except Exception as e:
                logger.error(f"Error in snapshot worker: {e}")
            
            # Sleep until next snapshot
            time.sleep(self.snapshot_interval)

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
        # Basic printer device info
        device_info = {
            "identifiers": ["anycubic_printer"],
            "name": getattr(self, 'printer_model', 'Anycubic Printer'),
            "model": getattr(self, 'printer_model', 'Unknown'),
            "manufacturer": "Anycubic"
        }
        
        # Light entity configuration
        light_config = {
            "name": "Printer Light",
            "unique_id": "anycubic_printer_light",
            "state_topic": "homeassistant/light/anycubic_printer_light/state",
            "command_topic": "homeassistant/light/anycubic_printer_light/set",
            "brightness_state_topic": "homeassistant/light/anycubic_printer_light/brightness",
            "brightness_command_topic": "homeassistant/light/anycubic_printer_light/brightness/set",
            "brightness_scale": 255,
            "on_command_type": "brightness",
            "payload_on": "ON",
            "payload_off": "OFF",
            "optimistic": False,
            "qos": 0,
            "device": device_info
        }
        
        # Publish light entity configuration
        self.ha_client.publish(
            "homeassistant/light/anycubic_printer_light/config", 
            json.dumps(light_config), 
            retain=True
        )
        logger.info("Created printer light entity in Home Assistant")
        
        # Subscribe to command topics
        self.ha_client.subscribe("homeassistant/light/anycubic_printer_light/set")
        self.ha_client.subscribe("homeassistant/light/anycubic_printer_light/brightness/set")
        
        # Set up message handler for Home Assistant commands
        self.ha_client.message_callback_add(
            "homeassistant/light/anycubic_printer_light/set", 
            self._on_ha_light_command
        )
        self.ha_client.message_callback_add(
            "homeassistant/light/anycubic_printer_light/brightness/set", 
            self._on_ha_brightness_command
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
            logger.info(f"Received brightness command from Home Assistant: {brightness_ha}")
            
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
                brightness = getattr(self, 'light_brightness', 100)
            else:
                # Save the brightness for future use
                self.light_brightness = brightness
            
            # Create request payload
            light_request = {
                "type": "light",
                "action": "control",
                "timestamp": int(time.time() * 1000),
                "msgid": message_id,
                "data": {
                            "type": 2,
                            "status": status,
                            "brightness": brightness
                        }
            }
            
            # Send the request
            logger.info(f"Setting printer light: status={status}, brightness={brightness}")
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
                if hasattr(self, 'message_handlers'):
                    for handler in self.message_handlers:
                        if handler(msg.topic, data):
                            return  # Message was handled, stop processing
                        
                # If this is a light report message
                if "type" in data and data["type"] == "light":
                    try:
                        # First, check if this is a response to a light control command
                        if data.get("action") == "control" and data.get("state") == "done":
                            logger.info(f"Light control command completed with code: {data.get('code')}")
                            # Refresh light status to get current state
                            self._query_light_status()
                            return
                            
                        # Extract light data with better error checking
                        if "data" in data and data["data"] is not None and "lights" in data["data"] and data["data"]["lights"] is not None and len(data["data"]["lights"]) > 0:
                            light_data = data["data"]["lights"][0]
                            light_status = light_data.get("status", 0)
                            light_brightness = light_data.get("brightness", 0)
                            light_type = light_data.get("type", 0)
                            
                            logger.info(f"Received light status: on={light_status==1}, brightness={light_brightness}, type={light_type}")
                            
                            # Create light entity if it doesn't exist yet
                            if not hasattr(self, 'light_entity_created') or not self.light_entity_created:
                                self._create_light_entity()
                            
                            # Publish state to Home Assistant
                            self.ha_client.publish(
                                "homeassistant/light/anycubic_printer_light/state", 
                                "ON" if light_status == 1 else "OFF", 
                                retain=True
                            )
                            
                            # Publish brightness level (0-255 for Home Assistant)
                            brightness = int(min(255, max(0, (light_brightness * 255) / 100)))
                            self.ha_client.publish(
                                "homeassistant/light/anycubic_printer_light/brightness", 
                                str(brightness), 
                                retain=True
                            )
                            
                            return  # Successfully handled
                        else:
                            logger.debug(f"Light message did not contain expected data structure: {json.dumps(data)}")
                    except Exception as e:
                        logger.error(f"Error processing light data: {e}")
                        logger.debug(traceback.format_exc())
                
                # If this is an info report message with printer status
                if "type" in data and data["type"] == "info":
                    printer_data = data['data']
                    device_name = printer_data.get('printerName', 'Anycubic Printer')
                    device_model = printer_data.get('model', 'Unknown')
                    
                    # Define the device for all sensors
                    device_info = {
                        "identifiers": ["anycubic_printer"],
                        "name": device_name,
                        "model": device_model,
                        "manufacturer": "Anycubic",
                        "sw_version": printer_data.get('version', 'Unknown')
                    }
                    
                    # Create individual sensors
                    sensors = []
                    
                    # State sensor
                    if 'state' in printer_data:
                        sensors.append({
                            "name": "Printer State",
                            "unique_id": "anycubic_printer_state",
                            "state_topic": "homeassistant/sensor/anycubic_printer/state",
                            "value_template": "{{ value_json.state }}",
                            "icon": "mdi:printer-3d",
                            "device": device_info
                        })
                    
                    # Temperature sensors
                    if 'temp' in printer_data:
                        temp = printer_data['temp']
                        # Current hotbed temperature
                        sensors.append({
                            "name": "Hotbed Temperature",
                            "unique_id": "anycubic_hotbed_temp",
                            "state_topic": "homeassistant/sensor/anycubic_printer/state",
                            "value_template": "{{ value_json.hotbed_temp }}",
                            "unit_of_measurement": "°C",
                            "device_class": "temperature",
                            "icon": "mdi:printer-3d-nozzle-heat",
                            "device": device_info
                        })
                        
                        # Current nozzle temperature
                        sensors.append({
                            "name": "Nozzle Temperature",
                            "unique_id": "anycubic_nozzle_temp",
                            "state_topic": "homeassistant/sensor/anycubic_printer/state",
                            "value_template": "{{ value_json.nozzle_temp }}",
                            "unit_of_measurement": "°C",
                            "device_class": "temperature",
                            "icon": "mdi:printer-3d-nozzle-heat",
                            "device": device_info
                        })
                    
                    # Fan speed sensors
                    if 'fan_speed_pct' in printer_data:
                        sensors.append({
                            "name": "Fan Speed",
                            "unique_id": "anycubic_fan_speed",
                            "state_topic": "homeassistant/sensor/anycubic_printer/state",
                            "value_template": "{{ value_json.fan_speed_pct }}",
                            "unit_of_measurement": "%",
                            "icon": "mdi:fan",
                            "device": device_info
                        })
                    
                    if 'aux_fan_speed_pct' in printer_data:
                        sensors.append({
                            "name": "Aux Fan Speed",
                            "unique_id": "anycubic_aux_fan_speed",
                            "state_topic": "homeassistant/sensor/anycubic_printer/state",
                            "value_template": "{{ value_json.aux_fan_speed_pct }}",
                            "unit_of_measurement": "%",
                            "icon": "mdi:fan",
                            "device": device_info
                        })
                    
                    # Print speed mode
                    if 'print_speed_mode' in printer_data:
                        sensors.append({
                            "name": "Print Speed Mode",
                            "unique_id": "anycubic_print_speed_mode",
                            "state_topic": "homeassistant/sensor/anycubic_printer/state",
                            "value_template": "{{ value_json.print_speed_mode }}",
                            "icon": "mdi:speedometer",
                            "device": device_info
                        })
                    
                    # IP Address sensor
                    if 'ip' in printer_data:
                        sensors.append({
                            "name": "Printer IP Address",
                            "unique_id": "anycubic_printer_ip",
                            "state_topic": "homeassistant/sensor/anycubic_printer/state",
                            "value_template": "{{ value_json.ip_address }}",
                            "icon": "mdi:ip-network",
                            "device": device_info
                        })
                    
                    # Store the stream URL for snapshot use if available
                    if 'urls' in printer_data and 'rtspUrl' in printer_data['urls']:
                        self.stream_url = printer_data['urls']['rtspUrl']
                        
                        # Add a URL sensor
                        sensors.append({
                            "name": "Printer Camera URL",
                            "unique_id": "anycubic_printer_camera_url",
                            "state_topic": "homeassistant/sensor/anycubic_printer/state",
                            "value_template": "{{ value_json.camera_url }}",
                            "icon": "mdi:cctv",
                            "device": device_info
                        })
                        
                        # Create a simple image entity for the snapshot
                        image_config = {
                            "name": "Anycubic Snapshot",
                            "unique_id": "anycubic_printer_snapshot",
                            "state_topic": "homeassistant/image/anycubic_printer_snapshot/state",
                            "image_topic": "homeassistant/image/anycubic_printer_snapshot/image",
                            "content_type": "image/jpeg",
                            "device": device_info
                        }
                        
                        # Publish image entity config
                        self.ha_client.publish(
                            "homeassistant/image/anycubic_printer_snapshot/config", 
                            json.dumps(image_config), 
                            retain=True
                        )
                        logger.info("Published snapshot image entity config")
                        
                        # Start snapshot thread if not already running
                        if self.snapshot_thread is None or not self.snapshot_thread.is_alive():
                            self.snapshot_thread = Thread(target=self.snapshot_worker)
                            self.snapshot_thread.daemon = True
                            self.snapshot_thread.start()
                            logger.info(f"Started snapshot thread with interval {self.snapshot_interval}s")
                    
                    # Publish all sensor configurations
                    for sensor in sensors:
                        discovery_topic = f"homeassistant/sensor/{sensor['unique_id']}/config"
                        self.ha_client.publish(discovery_topic, json.dumps(sensor), retain=True)
                    
                    # Create the combined state message
                    state_data = {
                        "state": printer_data.get('state', 'unknown'),
                        "hotbed_temp": printer_data.get('temp', {}).get('curr_hotbed_temp', 0),
                        "nozzle_temp": printer_data.get('temp', {}).get('curr_nozzle_temp', 0),
                        "target_hotbed_temp": printer_data.get('temp', {}).get('target_hotbed_temp', 0),
                        "target_nozzle_temp": printer_data.get('temp', {}).get('target_nozzle_temp', 0),
                        "fan_speed_pct": printer_data.get('fan_speed_pct', 0),
                        "aux_fan_speed_pct": printer_data.get('aux_fan_speed_pct', 0),
                        "print_speed_mode": printer_data.get('print_speed_mode', 0),
                        "ip_address": printer_data.get('ip', ''),
                        "camera_url": printer_data.get('urls', {}).get('rtspUrl', ''),
                        "last_updated": time.strftime("%Y-%m-%d %H:%M:%S")
                    }
                    # log the state data
                    logger.info(f"State data: {state_data}")
                    # Publish the state
                    self.ha_client.publish(
                        "homeassistant/sensor/anycubic_printer/state", 
                        json.dumps(state_data), 
                        retain=True
                    )
                    logger.info("Published printer state data to Home Assistant")
                else:
                    # Handle other types of messages
                    topic_parts = msg.topic.split('/')
                    if len(topic_parts) > 2:
                        topic_type = topic_parts[-2]
                        topic_action = topic_parts[-1]
                        
                        # Check if this is a system message with a hash-like ID (silently handle)
                        is_system_message = (
                            len(topic_type) == 32 and 
                            all(c in "0123456789abcdef" for c in topic_type.lower())
                        )
                        
                        if not is_system_message:
                            logger.info(f"Unhandled message type: {topic_type}/{topic_action}")
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
        encoded = urllib.parse.quote(urllib.parse.quote(second_md5, safe=''))
        
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
                from Crypto.Cipher import AES
                from Crypto.Util.Padding import unpad
            except ImportError:
                # Try alternative path used by some Linux distributions
                try:
                    from Cryptodome.Cipher import AES
                    from Cryptodome.Util.Padding import unpad
                    logger.info("Using Cryptodome instead of Crypto")
                except ImportError:
                    raise ImportError("Cannot find PyCryptodome or PyCryptodomex")
            
            import base64
            
            # Extract the key from token (second half)
            key = token[16:32].encode('utf-8')
            
            # Use the local token as IV
            iv = local_token.encode('utf-8')
            
            # Ensure IV is 16 bytes (pad or truncate)
            if len(iv) < 16:
                iv = iv + (b'\0' * (16 - len(iv)))
            else:
                iv = iv[:16]
            
            # Decode the base64 encoded data
            encrypted_bytes = base64.b64decode(encrypted_data)
            
            # Create the cipher
            cipher = AES.new(key, AES.MODE_CBC, iv)
            
            # Decrypt and unpad
            decrypted_bytes = unpad(cipher.decrypt(encrypted_bytes), AES.block_size)
            
            # Convert to string and parse JSON
            decrypted_text = decrypted_bytes.decode('utf-8')
            return json.loads(decrypted_text)
        
        except ImportError:
            logger.error("Cannot decrypt printer data: PyCryptodome library not installed")
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
                    if 'modelId' in data:
                        self.printer_mode_id = data['modelId']
                    if 'modelName' in data:
                        self.printer_model = data['modelName']
                    
                    # Step 2: Get detailed printer info by calling the control URL
                    if 'ctrlInfoUrl' in data and 'token' in data:
                        ctrl_url = data['ctrlInfoUrl']
                        token = data['token']
                        
                        # Current timestamp in milliseconds
                        ts = int(time.time() * 1000)
                        
                        # Generate a random nonce (6 characters)
                        import random
                        import string
                        nonce = ''.join(random.choices(string.ascii_letters + string.digits, k=6))

                        #Generate PC DID
                        pc_did = ''.join(random.choices(string.ascii_uppercase + string.digits, k=32))
                        
                        # Generate sign parameter using the correct algorithm
                        sign = self.generate_sign(token, ts, nonce)
                        
                        # Make the control request with all required parameters
                        logger.info(f"Making control request to {ctrl_url}")
                        
                        ctrl_params = {
                            'ts': ts,
                            'nonce': nonce,
                            'sign': sign,
                            'did': pc_did
                        }
                        
                        # Make the POST request
                        ctrl_response = requests.post(ctrl_url, params=ctrl_params, timeout=5)
                        
                        if ctrl_response.status_code == 200:
                            ctrl_data = ctrl_response.json()
                            logger.info(f"Received control response with status code: {ctrl_data.get('code')}")
                            
                            # Check if we got a success response
                            if ctrl_data.get('code') == 200 and ctrl_data.get('message') == 'success':
                                # Process the response data
                                if 'data' in ctrl_data and 'info' in ctrl_data['data']:
                                    # Store the token if available
                                    if 'token' in ctrl_data['data']:
                                        local_token = ctrl_data['data']['token']
                                        logger.info(f"Received printer token: {local_token}")
                                        
                                        # Get the encrypted printer data
                                        printer_encrypted_data = ctrl_data['data']['info']
                                        logger.info("Received encrypted printer data, decrypting...")
                                        
                                        # Decrypt the data
                                        printer_data = self.decrypt_printer_data(
                                            printer_encrypted_data,
                                            token,  # Original token from HTTP request
                                            local_token
                                        )
                                        
                                        if printer_data:
                                            logger.info(f"Successfully decrypted printer data")
                                            
                                            # Extract connection parameters from decrypted data
                                            if 'broker' in printer_data:
                                                broker_url = printer_data['broker']
                                                # Parse the broker URL (format: mqtts://192.168.86.59:9883)
                                                import re
                                                broker_match = re.match(r'mqtts?://([^:]+):(\d+)', broker_url)
                                                if (broker_match):
                                                    # Update the global broker settings
                                                    ANYCUBIC_BROKER = broker_match.group(1)
                                                    ANYCUBIC_PORT = int(broker_match.group(2))
                                                    logger.info(f"Updated broker settings: {ANYCUBIC_BROKER}:{ANYCUBIC_PORT}")
                                            
                                            # Extract authentication info
                                            if 'username' in printer_data:
                                                ANYCUBIC_USER = printer_data['username']
                                                logger.info(f"Using discovered username: {ANYCUBIC_USER}")
                                            
                                            if 'password' in printer_data:
                                                ANYCUBIC_PASS = printer_data['password']
                                                logger.info("Using discovered password")
                                            
                                            # Extract device ID and mode ID
                                            if 'deviceId' in printer_data:
                                                self.printer_device_id = printer_data['deviceId']
                                                logger.info(f"Using discovered device ID: {self.printer_device_id}")
                                            
                                            if 'modeId' in printer_data:
                                                self.printer_mode_id = printer_data['modeId']
                                                logger.info(f"Using discovered mode ID: {self.printer_mode_id}")
                                            
                                            if 'modelName' in printer_data:
                                                self.printer_model = printer_data['modelName']
                                                logger.info(f"Using discovered model name: {self.printer_model}")
                                            
                                            # Also save the certificate and private key if needed for future use
                                            if 'devicecrt' in printer_data and 'devicepk' in printer_data:
                                                logger.info("Discovered device certificate and private key")
                                            
                                            # Return success instead of calling exit()
                                            return True
                            else:
                                logger.warning(f"Control request returned error: {ctrl_data}")
                        else:
                            logger.warning(f"Control request failed with status code: {ctrl_response.status_code}")
                    
                    # If we have the necessary information, consider discovery successful
                    if self.printer_mode_id and self.printer_device_id:
                        logger.info(f"Discovery successful: Model={self.printer_model}, ModeID={self.printer_mode_id}, DeviceID={self.printer_device_id}")
                        return True
                    else:
                        logger.warning("Discovery incomplete - missing mode ID or device ID")
                        return False
                else:
                    logger.warning(f"HTTP discovery request failed with status code: {response.status_code}")
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
    
    bridge = AnycubicMqttBridge()
    if not bridge.connect():
        logger.error("Failed to establish initial connections, exiting")
        return 1
    
    logger.info("Anycubic MQTT bridge is running")
    
    try:
        # Keep the main thread running
        while running:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        bridge.disconnect()
        logger.info("Bridge stopped")
    
    return 0

if __name__ == "__main__":
    sys.exit(main())
