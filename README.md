# Anycubic S1 MQTT Bridge

This project provides a bridge between Anycubic 3D printers and Home Assistant using MQTT. It allows for monitoring and controlling the printer through Home Assistant, enabling features such as printer status updates, light control, and video streaming.

## Table of Contents

- [Installation](#installation)
- [Usage](#usage)
- [Configuration](#configuration)
- [Docker Setup](#docker-setup)
- [Contributing](#contributing)
- [License](#license)

## Installation

1. Clone the repository:

   ```
   git clone https://github.com/yourusername/anycubic-s1-mqtt-bridge.git
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
- `HA_USER`: Your Home Assistant username.
- `HA_PASS`: Your Home Assistant password.
- `SNAPSHOT_INTERVAL_IDLE`: Interval for taking snapshots when idle.
- `SNAPSHOT_INTERVAL_BUSY`: Interval for taking snapshots when busy.
- `INFO_UPDATE_INTERVAL`: Interval for updating printer information.

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

## Contributing

Contributions are welcome! Please open an issue or submit a pull request for any improvements or bug fixes.

## License

This project is licensed under the MIT License. See the LICENSE file for more details.
