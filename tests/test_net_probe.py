# net_probe.py against its spec: pings every target each run, before any heavy test; heavy
# tests run one at a time in config order (here speed test, pve2 iperf3, VPS iperf3, each
# iperf3 both ways), only when their latest slot (speed test 03/09/15/21, pve2 01/07/13/19,
# VPS 04) is after their last success, so once per slot; a busy path or a busy iperf3 server
# skips the test until the next run; a failed busy check tests anyway; a target not answering
# pings skips its iperf3 untried and records it down; skips and errors never fail the run; the
# textfile keeps each probe's last results between runs and carries the plan speed.
# Usage:
#   python3 -m unittest discover -s tests

import calendar
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "net_probe.py"
spec = importlib.util.spec_from_file_location("net_probe", SCRIPT)
net_probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(net_probe)
net_probe.log = lambda message: None

# Slots are local hours; the tests' times are UTC
os.environ["TZ"] = "UTC"
time.tzset()

WAN = 'rate(node_network_receive_bytes_total{instance="router"}[5m]) * 8'
PVE2 = 'rate(node_network_receive_bytes_total{instance="pve2"}[5m]) * 8'
CONFIG = {
  "victoriametrics": {"query_url": "http://metrics.example.com:8428/api/v1/query"},
  "isp": {"down_mbps": 1000, "up_mbps": 100},
  "targets": {"pve2": "192.168.2.20", "vps": "100.64.0.2"},
  "tests": [
    {
      "test": "speedtest",
      "slots": [3, 9, 15, 21],
      "busy": [
        {"query": WAN, "limit_bps": 3e8},
        {"query": WAN.replace("receive", "transmit"), "limit_bps": 3e7},
      ],
    },
    {
      "test": "iperf",
      "path": "pve2",
      "slots": [1, 7, 13, 19],
      "busy": [
        {"query": PVE2, "limit_bps": 2e8},
        {"query": PVE2.replace("receive", "transmit"), "limit_bps": 2e8},
      ],
    },
    {"test": "iperf", "path": "vps", "slots": [4]},
  ],
}
NAMES = {"192.168.2.20": "pve2", "100.64.0.2": "vps"}


class Fake:
  """Records each call as a line; faults: pve2_off, wan_busy, pve2_busy, query_down,
  vps_server_busy, speedtest_fails."""

  def __init__(self, faults):
    self.faults = set(faults)
    self.calls = []

  def ping(self, address):
    self.calls.append(f"ping {NAMES[address]}")
    return (1.0, None) if NAMES[address] == "pve2" and "pve2_off" in self.faults else (0.0, 0.002)

  def query(self, expr):
    target = "wan" if "router" in expr else "pve2"
    self.calls.append(f"busy {target}")
    if "query_down" in self.faults:
      raise OSError("connection refused")
    return [5e8 if f"{target}_busy" in self.faults else 1e6]

  def speedtest(self):
    self.calls.append("speedtest")
    if "speedtest_fails" in self.faults:
      raise RuntimeError("no servers")
    return {
      "download": {"bandwidth": 1e8},
      "upload": {"bandwidth": 1.25e7},
      "ping": {"latency": 9.5, "jitter": 1.5},
      "packetLoss": 0,
      "server": {"name": "Sonic", "location": "San Jose, CA"},
      "isp": 'AT"T',
    }

  def iperf(self, address, reverse):
    path = NAMES[address]
    self.calls.append(f"iperf {path} {'down' if reverse else 'up'}")
    if path == "vps" and "vps_server_busy" in self.faults:
      return {"error": "the server is busy running a test. try again later"}
    rate = {"pve2": 9e8, "vps": 2e8}[path] / (2 if reverse else 1)
    return {"end": {"sum_received": {"bits_per_second": rate}, "sum_sent": {"retransmits": 3}}}


class NetProbeTest(unittest.TestCase):
  def setUp(self):
    tmp = tempfile.TemporaryDirectory()
    self.addCleanup(tmp.cleanup)
    self.dir = Path(tmp.name)
    self.config_path = self.dir / "config.json"
    self.write_config(CONFIG)

  def write_config(self, config):
    config = {
      **config,
      "state_path": str(self.dir / "state.json"),
      "metrics_path": str(self.dir / "prom"),
    }
    self.config_path.write_text(json.dumps(config))

  def probe(self, at, *faults):
    """One run at HH:MM on 2026-10-24 (UTC); the calls it made."""
    tools = Fake(faults)
    now = calendar.timegm((2026, 10, 24, *map(int, at.split(":")), 0))
    net_probe.run(net_probe.load_config(self.config_path), now, tools)
    return tools.calls

  def samples(self, metric):
    """The textfile's samples whose name starts with metric, without HELP and TYPE."""
    return [
      line
      for line in (self.dir / "prom").read_text().splitlines()
      if line.startswith(metric) and line[len(metric)] in "{ "
    ]

  def test_first_run_pings_then_runs_every_test_in_order(self):
    self.assertEqual(
      self.probe("03:05"),
      [
        "ping pve2",
        "ping vps",
        "busy wan",
        "busy wan",
        "speedtest",
        "busy pve2",
        "busy pve2",
        "iperf pve2 up",
        "iperf pve2 down",
        "iperf vps up",
        "iperf vps down",
      ],
    )
    self.assertEqual(
      self.samples("homelab_speedtest_bits_per_second"),
      [
        'homelab_speedtest_bits_per_second{direction="down"} 800000000',
        'homelab_speedtest_bits_per_second{direction="up"} 100000000',
      ],
    )
    self.assertEqual(
      self.samples("homelab_speedtest_info"),
      ['homelab_speedtest_info{server="Sonic (San Jose, CA)",isp="AT\\"T"} 1'],
    )
    self.assertEqual(
      self.samples("homelab_iperf_bits_per_second"),
      [
        'homelab_iperf_bits_per_second{path="pve2",direction="up"} 900000000',
        'homelab_iperf_bits_per_second{path="pve2",direction="down"} 450000000',
        'homelab_iperf_bits_per_second{path="vps",direction="up"} 200000000',
        'homelab_iperf_bits_per_second{path="vps",direction="down"} 100000000',
      ],
    )
    self.assertEqual(
      self.samples("homelab_isp_plan_bits_per_second"),
      [
        'homelab_isp_plan_bits_per_second{direction="down"} 1000000000',
        'homelab_isp_plan_bits_per_second{direction="up"} 100000000',
      ],
    )
    self.assertEqual(
      self.samples("homelab_ping_rtt_seconds"),
      ['homelab_ping_rtt_seconds{path="pve2"} 0.002', 'homelab_ping_rtt_seconds{path="vps"} 0.002'],
    )

  def test_each_test_runs_once_per_slot_and_keeps_its_results(self):
    self.probe("03:05")
    # 04:00 is the VPS's slot; the speed test (03:00) and pve2 (01:00) already succeeded since
    self.assertEqual(
      self.probe("04:05"), ["ping pve2", "ping vps", "iperf vps up", "iperf vps down"]
    )
    self.assertEqual(self.probe("05:05"), ["ping pve2", "ping vps"])
    self.assertEqual(len([c for c in self.probe("07:05") if c.startswith("iperf")]), 2)
    self.assertTrue(
      self.samples("homelab_speedtest_bits_per_second"), "dropped the 03:05 speed test"
    )

  def test_busy_wan_defers_the_speed_test_to_the_next_hour(self):
    self.assertNotIn("speedtest", self.probe("03:05", "wan_busy"))
    self.assertEqual(
      self.samples("homelab_net_probe_skipped_total"),
      ['homelab_net_probe_skipped_total{probe="speedtest",reason="busy"} 1'],
    )
    self.assertIn("speedtest", self.probe("04:05"), "no retry an hour later")
    # 04:05 succeeded, so the 03:00 slot is done
    self.assertNotIn("speedtest", self.probe("05:05"), "ran twice in one slot")

  def test_busy_pve2_skips_only_its_test(self):
    self.assertEqual(
      [c for c in self.probe("03:05", "pve2_busy") if c.startswith(("iperf", "speedtest"))],
      ["speedtest", "iperf vps up", "iperf vps down"],
    )

  def test_failed_busy_check_tests_anyway(self):
    self.assertIn("speedtest", self.probe("03:05", "query_down"))

  def test_unreachable_target_skips_its_iperf_untried_and_marks_it_down(self):
    self.probe("03:05")
    self.assertNotIn("iperf pve2 up", self.probe("07:05", "pve2_off"))
    self.assertEqual(
      [
        s
        for s in self.samples("homelab_iperf_up") + self.samples("homelab_iperf_bits_per_second")
        if "pve2" in s
      ],
      ['homelab_iperf_up{path="pve2"} 0'],
    )
    self.assertEqual(
      self.samples("homelab_ping_loss_ratio"),
      ['homelab_ping_loss_ratio{path="pve2"} 1', 'homelab_ping_loss_ratio{path="vps"} 0'],
    )

  def test_busy_server_and_failures_retry_without_failing_the_run(self):
    self.probe("03:05", "vps_server_busy", "speedtest_fails")
    self.assertEqual(
      self.samples("homelab_net_probe_skipped_total"),
      [
        'homelab_net_probe_skipped_total{probe="iperf_vps",reason="server_busy"} 1',
        'homelab_net_probe_skipped_total{probe="speedtest",reason="error"} 1',
      ],
    )
    self.assertEqual(
      [c for c in self.probe("04:05") if c.startswith(("iperf", "speedtest"))],
      ["speedtest", "iperf vps up", "iperf vps down"],
    )

  def test_metric_prefix_names_every_metric(self):
    self.write_config({**CONFIG, "metric_prefix": "lab"})
    self.probe("03:05")
    names = {
      line.split("{")[0].split(" ")[0]
      for line in (self.dir / "prom").read_text().splitlines()
      if not line.startswith("#")
    }
    self.assertTrue(names)
    self.assertEqual({n for n in names if not n.startswith("lab_")}, set())
    self.assertIn("lab_net_probe_last_success_timestamp_seconds", names)

  def test_tests_without_busy_checks_need_no_metrics_query(self):
    config = {k: v for k, v in CONFIG.items() if k != "victoriametrics"}
    self.write_config({**config, "tests": [{"test": "iperf", "path": "vps", "slots": [4]}]})
    self.assertEqual(
      self.probe("04:05"), ["ping pve2", "ping vps", "iperf vps up", "iperf vps down"]
    )

  def test_invalid_configs_are_rejected(self):
    cases = {
      "iperf path 'nas' isn't one of the targets": {
        "tests": [{"test": "iperf", "path": "nas", "slots": [1]}]
      },
      "test 'ping' isn't speedtest or iperf": {"tests": [{"test": "ping", "slots": [1]}]},
      "speedtest: slots must be hours 0 to 23": {"tests": [{"test": "speedtest", "slots": [24]}]},
      "speedtest is configured twice": {"tests": [{"test": "speedtest", "slots": [1]}] * 2},
      "missing targets": {"targets": None},
    }
    for message, change in cases.items():
      with self.subTest(message):
        config = {k: v for k, v in {**CONFIG, **change}.items() if v is not None}
        self.write_config(config)
        with self.assertRaises(net_probe.ConfigError) as raised:
          net_probe.load_config(self.config_path)
        self.assertTrue(str(raised.exception).endswith(message), raised.exception)
    config = {k: v for k, v in CONFIG.items() if k != "victoriametrics"}
    self.write_config(config)
    with self.assertRaisesRegex(
      net_probe.ConfigError, "speedtest has busy checks but there is no victoriametrics"
    ):
      net_probe.load_config(self.config_path)

  def test_example_config_is_valid(self):
    config = net_probe.load_config(SCRIPT.parent / "config.example.json")
    self.assertEqual([t["probe"] for t in config["tests"]], ["speedtest", "iperf_nas", "iperf_vps"])

  def test_cli_reports_a_bad_config_without_a_traceback(self):
    out = subprocess.run(
      [sys.executable, "-I", str(SCRIPT), "--config", str(self.dir / "missing.json")],
      stdout=subprocess.PIPE,
      stderr=subprocess.PIPE,
      text=True,
    )
    self.assertEqual(out.returncode, 1)
    self.assertTrue(out.stderr.startswith("net_probe: "), out.stderr)
    self.assertNotIn("Traceback", out.stderr)


if __name__ == "__main__":
  unittest.main()
