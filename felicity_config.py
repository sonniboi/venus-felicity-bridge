"""
Configuration for the Felicity Venus OS bridges.

All site-specific values live in an INI file. Every key has a default, and the
defaults reproduce the behaviour of the original single-site scripts, so a
missing file or a missing key never changes how the services behave - it only
means generic names and serial numbers are published.

Lookup order for the file:
  1. path given with --config on the command line
  2. environment variable FELICITY_BRIDGE_CONFIG
  3. /data/etc/felicity-bridge/config.ini   (persistent location on Venus OS)
  4. config.ini next to the scripts
"""
import configparser
import os

DEFAULT_PATHS = [
    "/data/etc/felicity-bridge/config.ini",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.ini"),
]

DEFAULT_VELIB_PATH = "/opt/victronenergy/dbus-systemcalc-py/ext/velib_python"


def _int_list(text):
    return [int(x) for x in str(text).replace(",", " ").split()]


class PackConfig(object):
    def __init__(self, number, section):
        self.number = number
        # Bus address of the pack on the shared RS485 line. Only used for the
        # informational /Mgmt/Connection text; the bridge itself reads the API.
        self.modbus_address = section.getint("modbus_address", fallback=number)
        self.serial = section.get("serial", fallback="felicity-%d" % number)
        self.device_instance = section.getint("device_instance", fallback=number)
        self.product_name = section.get("product_name", fallback="Felicity BMS Battery %d" % number)
        self.custom_name = section.get("custom_name", fallback="Felicity Battery %d" % number)
        self.serial_port = section.get("serial_port", fallback="/dev/ttyFelicityBMS")
        self.firmware_version = section.get("firmware_version", fallback="417")


class BridgeConfig(object):
    def __init__(self, parser=None, path=None):
        cp = parser or configparser.ConfigParser()
        self.path = path

        api = cp["api"] if cp.has_section("api") else cp["DEFAULT"]
        self.api_base_url = api.get("base_url", fallback="http://127.0.0.1:8080").rstrip("/")
        self.api_timeout = api.getint("timeout", fallback=5)
        self.poll_interval = api.getint("poll_interval", fallback=3)

        venus = cp["venus"] if cp.has_section("venus") else cp["DEFAULT"]
        self.velib_path = venus.get("velib_path", fallback=DEFAULT_VELIB_PATH)

        packs = cp["packs"] if cp.has_section("packs") else cp["DEFAULT"]
        self.pack_numbers = _int_list(packs.get("numbers", fallback="1 2 3"))
        # Packs that must answer before the bank is published. The others are
        # optional: if they drop out the bank keeps running with the rest.
        self.required_packs = _int_list(packs.get("required", fallback="1 2"))
        self.cells_per_pack = packs.getint("cells_per_pack", fallback=16)
        # Debounce for BMS fault bits: number of consecutive polls a fault must
        # be present before it is raised on D-Bus (20 polls x 3 s = 60 s).
        self.alarm_debounce_polls = packs.getint("alarm_debounce_polls", fallback=20)

        self.packs = {}
        for n in self.pack_numbers:
            name = "pack.%d" % n
            section = cp[name] if cp.has_section(name) else cp["DEFAULT"]
            self.packs[n] = PackConfig(n, section)

        bank = cp["bank"] if cp.has_section("bank") else cp["DEFAULT"]
        self.bank_device_instance = bank.getint("device_instance", fallback=512)
        self.bank_product_name = bank.get(
            "product_name", fallback="Felicity Battery Bank (%dx%dS)" % (len(self.pack_numbers), self.cells_per_pack))
        self.bank_custom_name = bank.get("custom_name", fallback="Felicity Battery Bank")
        self.bank_serial = bank.get("serial", fallback="COMBINED_BANK")
        self.bank_connection = bank.get("connection", fallback="Aggregated from 2x Felicity BMS")
        self.bank_firmware_version = bank.get("firmware_version", fallback="417")
        self.bank_poll_interval = bank.getint("poll_interval", fallback=5)
        self.pack_soc_ttl = bank.getint("pack_soc_ttl", fallback=900)
        self.register_timeout = bank.getint("register_timeout", fallback=90)
        self.default_capacity = bank.getfloat("default_capacity", fallback=200)

    def pack(self, number):
        if number not in self.packs:
            self.packs[number] = PackConfig(number, configparser.ConfigParser()["DEFAULT"])
        return self.packs[number]

    def api_url(self, number):
        return "%s/api/battery%d.json" % (self.api_base_url, number)


def load(path=None):
    """Load the configuration. Returns defaults if no file is found."""
    candidates = [path] if path else []
    env = os.environ.get("FELICITY_BRIDGE_CONFIG")
    if env:
        candidates.append(env)
    candidates.extend(DEFAULT_PATHS)
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            cp = configparser.ConfigParser()
            with open(candidate) as fh:
                cp.read_file(fh)
            return BridgeConfig(cp, candidate)
    if path:
        raise FileNotFoundError(path)
    return BridgeConfig()


def config_path_from_argv(argv):
    """Return the value of --config <path> / --config=<path> and the remaining args."""
    rest, path, skip = [], None, False
    for i, arg in enumerate(argv):
        if skip:
            skip = False
            continue
        if arg == "--config" and i + 1 < len(argv):
            path, skip = argv[i + 1], True
        elif arg.startswith("--config="):
            path = arg.split("=", 1)[1]
        else:
            rest.append(arg)
    return path, rest
