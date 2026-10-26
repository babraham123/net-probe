# Arguments and the entry point.
# Usage:
#   net_probe [--config PATH]

import argparse
import sys
import time

from .commands import run
from .config import DEFAULT_CONFIG, ConfigError, load_config
from .tools import Tools


def main(argv=None):
  parser = argparse.ArgumentParser(
    prog="net_probe",
    description="Pings, speed tests and iperf3, into node_exporter's textfile collector.",
  )
  parser.add_argument(
    "--config", default=DEFAULT_CONFIG, help=f"JSON config (default {DEFAULT_CONFIG})"
  )
  args = parser.parse_args(argv)
  try:
    config = load_config(args.config)
  except ConfigError as e:
    sys.exit(f"net_probe: {e}")
  run(config, time.time(), Tools(config))


def entry():
  """The zipapp's entry point."""
  sys.exit(main(sys.argv[1:]))
