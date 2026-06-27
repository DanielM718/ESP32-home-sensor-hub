# Development Workflow

## First Clone

```sh
git clone <repository>
cd sensor_home
```

Install the toolchains needed for the work you are doing:

- ESP-IDF v6.0.1 for firmware development
- Python 3 and virtual environment support for backend development
- Raspberry Pi OS Lite 64-bit for production backend deployment

## Local Configuration

Copy examples before editing local secrets.

```sh
cp server/.env.example server/backend/.env
cp esp/ESP32_master/main/wifi_cred.example.h esp/ESP32_master/main/wifi_cred.h
cp esp/ESP32C3_SEN66_air_quality/main/app_config.example.h esp/ESP32C3_SEN66_air_quality/main/app_config.h
```

Local secret files are ignored by Git.

## Firmware Development

Build from the firmware project directory:

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

Flash with the serial port for the connected board:

```sh
idf.py -p /dev/ttyUSB0 flash monitor
```

Generated ESP-IDF files stay local and are ignored by Git.

## Backend Development

Create a virtual environment outside source control:

```sh
cd server
python3 -m venv backend/.venv
backend/.venv/bin/python -m pip install -r requirements.txt
backend/.venv/bin/python -m pytest backend/tests
```

The production installer is intended for Raspberry Pi OS:

```sh
sudo server/install.sh
```

Do not run the installer on the Mac development machine.

## Before Committing

Check for accidental generated files or secrets:

```sh
git status --short
git diff --check
```

Build or test the area you changed:

- Firmware: `idf.py build`
- Backend: `python -m pytest server/backend/tests`
- Deployment: run the relevant `server/scripts/verify_*.sh` script on the Pi
