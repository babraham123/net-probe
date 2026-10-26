# The config file.

import json
from pathlib import Path

DEFAULT_CONFIG = "/etc/net_probe/config.json"
DEFAULTS = {
  "state_path": "/var/lib/net_probe/state.json",
  "metrics_path": "/var/lib/node_exporter/textfile_collector/net_probe.prom",
  "metric_prefix": "homelab",
  "speedtest": "speedtest",
}


class ConfigError(Exception):
  pass


def load_config(path):
  """The config file merged over DEFAULTS, each test given its probe name."""
  try:
    config = {**DEFAULTS, **json.loads(Path(path).read_text())}
  except (OSError, ValueError) as e:
    raise ConfigError(f"{path}: {e}") from e
  for key in ("targets", "isp", "tests"):
    if key not in config:
      raise ConfigError(f"{path}: missing {key}")
  probes = set()
  for test in config["tests"]:
    if test.get("test") == "speedtest":
      test["probe"] = "speedtest"
    elif test.get("test") == "iperf":
      if test.get("path") not in config["targets"]:
        raise ConfigError(f"{path}: iperf path {test.get('path')!r} isn't one of the targets")
      test["probe"] = f"iperf_{test['path']}"
    else:
      raise ConfigError(f"{path}: test {test.get('test')!r} isn't speedtest or iperf")
    slots = test.get("slots")
    if not slots or not all(isinstance(h, int) and 0 <= h <= 23 for h in slots):
      raise ConfigError(f"{path}: {test['probe']}: slots must be hours 0 to 23")
    if test["probe"] in probes:
      raise ConfigError(f"{path}: {test['probe']} is configured twice")
    probes.add(test["probe"])
    test.setdefault("busy", [])
    if test["busy"] and "victoriametrics" not in config:
      raise ConfigError(f"{path}: {test['probe']} has busy checks but there is no victoriametrics")
  return config
