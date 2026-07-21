# Repository Layout

This repository is organized as one Git project rooted at `sensor_home/`.

```text
sensor_home/
├── esp/
│   ├── ESP32_master/
│   ├── ESP32C3_SHT41_node/
│   └── ESP32C3_SEN66_air_quality/
├── server/
├── home-assistant/
├── docs/
├── .editorconfig
├── .gitignore
└── README.md
```

## Root

The root owns repository-wide version control, documentation, and editor
configuration. Keep repository hygiene files here instead of duplicating them in
each project unless a tool has a project-specific technical requirement.

## ESP Projects

Each directory under `esp/` is an independent ESP-IDF firmware project with its
own `CMakeLists.txt` and `main/` source tree.

Commit:

- Source files in `main/`, `include/`, or project components
- `CMakeLists.txt`
- `idf_component.yml`
- `sdkconfig.defaults`
- Partition table CSV files
- Example configuration headers
- Documentation

Do not commit:

- `build/`
- `sdkconfig`
- `sdkconfig.old`
- `managed_components/`
- `dependencies.lock`
- Generated binaries, ELF files, and map files
- Local credential headers

## Server

The `server/` directory contains the Raspberry Pi backend, native install
scripts, systemd units, service configuration, and backend documentation.

Commit:

- Python source
- Flask templates and static source assets
- Install and verification scripts
- systemd unit templates
- Config templates and examples
- `requirements.txt`
- `.env.example`

Do not commit:

- `.env`
- Python virtual environments
- Python caches
- Local runtime logs

## Home Assistant

The `home-assistant/` directory contains repository-managed templates for an
independent Compose deployment. The Raspberry Pi installer copies these to
`/opt/home-assistant`; persistent configuration and credentials remain there,
outside `/opt/home-sensor/server`.

Commit Compose/configuration templates, discovery source/tests, operations
scripts, examples, and documentation. Do not commit `.env`, `secrets.yaml`,
`.storage`, Home Assistant databases/logs, or `discovery-data/`.

## VS Code

Local VS Code settings are ignored because ESP-IDF paths, serial ports, and
toolchain locations are machine-specific. Prefer documenting required tools in
the README instead of committing absolute paths.
