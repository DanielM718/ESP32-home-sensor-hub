# Sensor Home

Environmental monitoring system with ESP32 firmware projects and a Raspberry Pi
backend. The repository is intended to be cloned directly onto development
machines and onto the Raspberry Pi that runs the backend services.

## Repository Overview

```text
sensor_home/
├── esp/
│   ├── ESP32_master/                 ESP32 gateway: ESP-NOW receiver and MQTT publisher
│   ├── ESP32C3_SHT41_node/           ESP32-C3 SHT41 temperature/humidity node
│   └── ESP32C3_SEN66_air_quality/    ESP32-C3 SEN66 air quality node
├── server/                           Raspberry Pi backend and deployment scripts
├── docs/                             Repository-level development documentation
├── .editorconfig                     Shared editor formatting defaults
├── .gitignore                        Authoritative repository ignore rules
└── README.md
```

The repository keeps application source, configuration templates, deployment
scripts, and documentation under version control. Generated firmware builds,
local secrets, virtual environments, and editor machine state are ignored.

## Firmware Projects

All ESP projects are ESP-IDF projects.

- `esp/ESP32_master`: receives ESP-NOW packets from sensor nodes and publishes
  readings to MQTT on the Raspberry Pi.
- `esp/ESP32C3_SHT41_node`: battery sensor node for SHT41 temperature and
  humidity readings.
- `esp/ESP32C3_SEN66_air_quality`: SEN66 air quality node using I2C and
  ESP-NOW.

Use ESP-IDF v6.0.1 unless a project README says otherwise.

### Build Firmware

```sh
cd esp/ESP32_master
idf.py set-target esp32
idf.py build
```

```sh
cd esp/ESP32C3_SHT41_node
idf.py set-target esp32c3
idf.py build
```

```sh
cd esp/ESP32C3_SEN66_air_quality
idf.py set-target esp32c3
idf.py build
```

Flash and monitor with the serial port for the connected board:

```sh
idf.py -p /dev/ttyUSB0 flash monitor
```

On macOS the port may look like `/dev/tty.usbmodem1101` or
`/dev/tty.usbserial-0001`.

Local firmware secrets are copied from templates and are not committed:

```sh
cp esp/ESP32_master/main/wifi_cred.example.h esp/ESP32_master/main/wifi_cred.h
cp esp/ESP32C3_SEN66_air_quality/main/app_config.example.h esp/ESP32C3_SEN66_air_quality/main/app_config.h
```

## Raspberry Pi Backend

The backend lives in `server/` and includes:

- Mosquitto MQTT configuration
- Python MQTT-to-InfluxDB bridge
- Flask REST API and dashboard
- InfluxDB OSS v2 setup
- Grafana provisioning
- Tailscale remote-access documentation
- systemd units and install scripts

See [server/README.md](server/README.md) and the detailed documentation in
[server/docs](server/docs).

## Raspberry Pi Deployment

On a new Raspberry Pi:

```sh
git clone <repository>
cd sensor_home
sudo server/install.sh
```

The installer is intended for Raspberry Pi OS Lite 64-bit. Do not run the
installer on the Mac development machine.

After installation, use Git to deploy updates:

```sh
cd sensor_home
git pull
sudo server/install.sh
```

Firmware can also be built or flashed from the cloned repository when ESP-IDF is
installed on that machine.

## Development Workflow

1. Create or update local configuration files from the committed examples.
2. Make changes on a feature branch.
3. Build the affected ESP-IDF project or run the relevant backend tests.
4. Review `git status` before committing.
5. Open a pull request before merging into `main`.

Useful checks:

```sh
git status --short
python -m pytest server/backend/tests
```

## Git Workflow

Use `main` as the stable branch. Keep `main` deployable to the Raspberry Pi.

Use short-lived branches:

```text
feature/<short-description>
fix/<short-description>
docs/<short-description>
```

Write commit messages in the imperative mood:

```text
Add root repository documentation
Ignore ESP-IDF generated artifacts
Update Pi deployment notes
```

Tag stable releases after testing:

```sh
git tag -a v2026.06.27 -m "Raspberry Pi deployment baseline"
git push origin v2026.06.27
```

GitHub Actions are intentionally not configured yet. Hardware flashing, ESP-NOW,
MQTT, InfluxDB, Grafana, and Raspberry Pi services require local or
hardware-aware validation before CI rules are useful.

## Documentation

- [Repository layout](docs/REPOSITORY_LAYOUT.md)
- [Git workflow](docs/GIT_WORKFLOW.md)
- [Development workflow](docs/DEVELOPMENT.md)
- [Server documentation](server/docs)
