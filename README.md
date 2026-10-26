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
- In the Home Assistant add-on, MQTT broker settings are discovered from the
   Supervisor MQTT service; the `HA_*` values remain fallbacks for standalone use.

The Print Speed Mode sensor maps Anycubic values to `Silent`, `Standard`, or
`Sport`; the matching select can change modes while a print is active.

Each detected ACE Pro box and its filament sensors are grouped under a separate
Home Assistant child device linked to the printer. Box names use the firmware
index, so firmware ID `0` appears as `ACE Pro Box 0`.

`Printer State` reports leveling/preheating phases. During leveling,
`Print Progress` shows the leveling percentage alongside the `leveling` state.

The printer serial number is shown in Home Assistant device information; its IP
address remains available as a diagnostic entity.
Printer firmware and each ACE Pro box's firmware are also shown in their
respective Home Assistant device information when reported by the printer.

Target nozzle and hotbed temperatures are adjustable from Home Assistant, as
are the main, auxiliary, and ACE box fans. Temperature controls use `0–320 °C`
for the nozzle and `0–120 °C` for the bed; fan controls use `0–100%`.

The bridge also exposes `Job Failed` and `Print Problem` binary sensors from
Anycubic MQTT reports. Their attributes include the reported state, code,
action, and error message. The active sensors follow the latest MQTT report and
can clear on normal acknowledgements. `Last Print Error` keeps the latest error
details available afterward. Known
Anycubic messages are translated to Dutch; the original is retained in the
`original_message` attribute.

### Obico ML (Home Assistant add-on)

Install and start the Obico ML API add-on from
[mattjacobson6/ha-addons](https://github.com/mattjacobson6/ha-addons) (the
`obico-ml-api` folder). In this bridge add-on's configuration, enable
**Obico ML**. The bridge searches installed add-ons through the Supervisor API
for **Obico ML** and reads its internal hostname automatically. It then adds
`http://`, port `3333`, and `/detect/`. The hostname setting is a fallback if
Supervisor cannot find the add-on or its internal hostname cannot be reached.
This requires the add-on's `manager` Supervisor role. The bridge also checks
`/hc/` every 30 seconds and reports Obico entities unavailable when the API
cannot be reached.

Obico entities are grouped under a separate `Obico ML` child device in Home
Assistant. It provides retained sliders for the detection interval (`1–3600`
seconds; default `30`) and threshold (`0–1`, step `0.01`; default `0.2`); both
values are restored after add-on restarts. The bridge also registers a
communication switch, failure binary sensor, confidence sensor, and detection
image through MQTT discovery. Detection only runs while the printer state is
`busy`. When Obico detects a failure during `busy`, the bridge presses the
existing Pause Print button once for that failure.

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

The live FFmpeg camera is configured in Home Assistant, because add-ons cannot
create Home Assistant YAML camera entries through MQTT discovery. The bridge
still uses the stream URL internally for snapshot fallback and publishes
periodic images as an image entity. Configure the live camera in
`configuration.yaml` as below; use the printer IP from the add-on settings.

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
