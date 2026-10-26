# Anycubic S1 MQTT Bridge

This project provides a bridge between Anycubic Kobra S1 3D printers and Home Assistant using MQTT. It allows for monitoring and controlling the printer through Home Assistant, enabling features such as printer status updates, light control, print pause/resume/cancel, and video streaming.

## Table of Contents

- [Installation](#installation)
- [Usage](#usage)
- [Configuration](#configuration)
- [Docker Setup](#docker-setup)
- [Home Assistant Integration](#home-assistant-integration)
- [Contributing](#contributing)

## Installation

1. Clone the repository:

   ```
   git clone https://github.com/metheos/anycubic-s1-mqtt-bridge.git
   cd anycubic-s1-mqtt-bridge
   ```

2. Create a `.env` file based on the `.env.example` file and fill in the required environment variables.

3. Install the required Python packages:
   ```
   pip install -r requirements.txt
   ```

## Usage

To run the application, execute the following command:

```
python asm.py
```

Make sure your printer is connected and configured properly.

## Configuration

The application uses environment variables for configuration. You can set the following variables in your `.env` file:

- `ANYCUBIC_S1_IP`: Your S1 IP
- `HA_BROKER`: Your.HomeAssistant.MQTT.Address
- `HA_PORT`: 1883
- `HA_USER`: Your Home Assistant MQTT username.
- `HA_PASS`: Your Home Assistant MQTT password.
- `SNAPSHOT_INTERVAL_IDLE`: Interval for taking snapshots when printer is idle.
- `SNAPSHOT_INTERVAL_BUSY`: Interval for taking snapshots when printer is busy.
- `INFO_UPDATE_INTERVAL`: Interval for updating printer information.

For the Home Assistant add-on, the bridge reads the MQTT broker connection
from the Supervisor MQTT service automatically. The `mqtt_broker`, `mqtt_port`,
`mqtt_user`, and `mqtt_pass` settings are fallback values for standalone or
non-Supervisor deployments.

The bridge exposes adjustable nozzle and hotbed targets, main/auxiliary/ACE
box fan controls, and a Print Speed Mode select in Home Assistant. The speed
select is available while a print is active; the fan controls range from 0 to
100 percent.

Anycubic MQTT reports also create `Job Failed` and `Print Problem` binary
sensors. Their attributes include the reported state, code, action, MQTT type,
and error message. Known Anycubic messages are translated to Dutch; the source
text remains in `original_message`. Active sensors follow the latest MQTT report
and can clear on normal acknowledgements. `Last Print Error` retains the latest
error details afterward.

### Obico ML

Obico ML is optional and disabled by default. Install and start the Obico ML
API add-on from [mattjacobson6/ha-addons](https://github.com/mattjacobson6/ha-addons)
(the `obico-ml-api` folder), then enable Obico ML in this add-on's options. In
the Supervisor API, the bridge finds the installed **Obico ML** add-on and
reads its internal hostname automatically. It builds the API URL using HTTP,
port `3333`, and `/detect/`. The hostname field is only a fallback if
Supervisor cannot find Obico ML or its internal hostname is unreachable. This
requires the `manager` Supervisor role.
The bridge also checks `/hc/` every 30 seconds and marks Obico entities
unavailable when the backend cannot be reached.

When the printer camera is available, the bridge creates an Obico communication
switch, failure binary sensor, confidence sensor, detection image, threshold
slider, and interval slider in Home Assistant. Obico entities are grouped under
a separate `Obico ML` child device. The threshold ranges from `0` to `1` in
`0.01` steps (default `0.2`); the interval ranges from `1` to `3600` seconds
(default `30`). Both slider values are retained by MQTT across add-on restarts.
Detection only runs while the
printer state is `busy`. When Obico detects a failure during `busy`, the bridge
presses the existing Pause Print button once for that failure. It will not
send another pause until a clean detection or the printer leaves `busy`. The
Obico communication switch still pauses detection without restarting the
add-on.

## Docker Setup

To run the application using Docker, you can use the provided `Dockerfile` and `docker-compose.yml`.

1. Build the Docker image:

   ```
   docker-compose build
   ```

2. Start the application:
   ```
   docker-compose up
   ```

This will start the application in a Docker container, allowing for easy deployment and management.

## Home Assistant Integration

The repository includes an example Home Assistant view configuration that you can use to monitor your Anycubic S1 printer:

![Home Assistant View Example](HA_Example.PNG)

To use this view:

1. Copy the content from the [home_assistant_view_example.yaml](home_assistant_view_example.yaml) file
2. Paste it into the view yaml editor and click **Save**

This view uses elements from https://github.com/piitaya/lovelace-mushroom for the AMS spool visualization and badges.

The camera is manually configured in configuration.yaml as below

```
camera:
  - platform: ffmpeg
    name: "Anycubic S1 Printer Camera"
    input: "http://<Anycubic S1 IP>:18088/flv"
    extra_arguments: "-analyzeduration 1000000 -probesize 1000000 -fflags nobuffer -flags low_delay -strict experimental -fflags +genpts+discardcorrupt"
```

You may need to adjust entity IDs to match the ones created by your specific printer.

## Contributing

Contributions are welcome! Please open an issue or submit a pull request for any improvements or bug fixes.
