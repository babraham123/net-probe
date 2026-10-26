#!/usr/bin/env python3
# Network probe, run hourly by a systemd timer. Every run pings each target, then runs, one at
# a time and in config order, each heavy test whose slot has come: the Ookla speed test, and
# iperf3 to a target in both directions. A heavy test is due when its latest slot hour is after
# its last success, so a skipped one retries every run until it succeeds or its next slot
# comes. It skips while a VictoriaMetrics (or any Prometheus-compatible) instant query shows
# its path busy, so it neither disrupts real traffic nor reads it as a slow link. Results,
# skips and errors go to node_exporter's textfile collector; a probe never fails the run.
# Standard library only. The config is JSON; see config.example.json and README.md.
# Usage:
#   net_probe.py [--config PATH]

import argparse
import base64
import json
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

DEFAULT_CONFIG = "/etc/net_probe/config.json"
DEFAULTS = {
  "state_path": "/var/lib/net_probe/state.json",
  "metrics_path": "/var/lib/node_exporter/textfile_collector/net_probe.prom",
  "metric_prefix": "homelab",
  "speedtest": "speedtest",
}
# Metric names after the prefix, in exposition order
HELP = {
  "ping_loss_ratio": ("gauge", "Share of 20 pings to the path that got no reply."),
  "ping_rtt_seconds": ("gauge", "Average round trip of the replied pings."),
  "speedtest_bits_per_second": (
    "gauge",
    "Ookla speed test throughput; direction down is the download.",
  ),
  "isp_plan_bits_per_second": ("gauge", "The ISP plan's advertised speed (config isp)."),
  "speedtest_latency_seconds": ("gauge", "Ookla speed test idle latency."),
  "speedtest_jitter_seconds": ("gauge", "Ookla speed test idle jitter."),
  "speedtest_packet_loss_ratio": (
    "gauge",
    "Ookla speed test packet loss, when the server reports it.",
  ),
  "speedtest_info": ("gauge", "The last speed test's server and ISP."),
  "iperf_up": ("gauge", "1 when the path's iperf3 server answered, 0 when it was unreachable."),
  "iperf_bits_per_second": (
    "gauge",
    "iperf3 throughput as received; direction up is from this host.",
  ),
  "iperf_retransmits": ("gauge", "TCP retransmits by the iperf3 sender."),
  "net_probe_last_attempt_timestamp_seconds": (
    "gauge",
    "Unix time the probe last ran or was skipped.",
  ),
  "net_probe_last_success_timestamp_seconds": ("gauge", "Unix time the probe last succeeded."),
  "net_probe_skipped_total": ("counter", "Probes skipped or failed, by reason."),
}


class ConfigError(Exception):
  pass


class Skip(Exception):
  """A probe that didn't measure: reason labels the skip, samples replace its last ones."""

  def __init__(self, reason, samples=None):
    super().__init__(reason)
    self.reason = reason
    self.samples = samples


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


class Tools:
  """The commands and the metrics query; tests substitute their own."""

  def __init__(self, config):
    self.speedtest_command = config["speedtest"]
    self.vm = config.get("victoriametrics", {})
    self.authorization = None

  def ping(self, address):
    """(loss ratio, average rtt in seconds or None) over 20 pings."""
    out = subprocess.run(
      ["ping", "-c", "20", "-i", "0.2", "-W", "1", "-q", address],
      text=True,
      stdout=subprocess.PIPE,
      stderr=subprocess.STDOUT,
      timeout=60,
    ).stdout
    counts = re.search(r"(\d+) packets transmitted, (\d+) received", out)
    if not counts or not int(counts[1]):
      return 1.0, None
    rtt = re.search(r"= [\d.]+/([\d.]+)/", out)
    return 1 - int(counts[2]) / int(counts[1]), float(rtt[1]) / 1000 if rtt else None

  def speedtest(self):
    command = [self.speedtest_command, "--format=json", "--accept-license", "--accept-gdpr"]
    return json.loads(
      subprocess.run(command, check=True, text=True, stdout=subprocess.PIPE, timeout=180).stdout
    )

  def iperf(self, address, reverse):
    """iperf3's JSON report; on failure it carries only an error."""
    command = ["iperf3", "-c", address, "-J", "-t", "10", "-O", "2", "--connect-timeout", "3000"]
    out = subprocess.run(
      command + (["-R"] if reverse else []), text=True, stdout=subprocess.PIPE, timeout=60
    ).stdout
    return json.loads(out)

  def basic_auth(self):
    """The Authorization header value, or "" without a password."""
    if "password_command" in self.vm:
      password = subprocess.run(
        self.vm["password_command"], check=True, text=True, stdout=subprocess.PIPE
      ).stdout.rstrip("\n")
    elif "password_file" in self.vm:
      password = Path(self.vm["password_file"]).read_text().rstrip("\n")
    else:
      return ""
    credentials = f"{self.vm.get('user', 'admin')}:{password}".encode()
    return "Basic " + base64.b64encode(credentials).decode()

  def query(self, expr):
    """The values of an instant query."""
    if self.authorization is None:
      self.authorization = self.basic_auth()
    headers = {"Authorization": self.authorization} if self.authorization else {}
    url = f"{self.vm['query_url']}?{urllib.parse.urlencode({'query': expr})}"
    with urllib.request.urlopen(
      urllib.request.Request(url, headers=headers), timeout=10
    ) as response:
      return [float(r["value"][1]) for r in json.load(response)["data"]["result"]]


def log(message):
  print(message, flush=True)


def latest_slot(now, hours):
  """Unix time of the latest slot hour at or before now, in local time."""
  local = datetime.fromtimestamp(now)
  past = [
    local.replace(hour=h, minute=0, second=0, microsecond=0) for h in hours if h <= local.hour
  ]
  if not past:
    past = [(local - timedelta(days=1)).replace(hour=max(hours), minute=0, second=0, microsecond=0)]
  return max(past).timestamp()


def busy(tools, checks):
  for check in checks:
    try:
      values = tools.query(check["query"])
    except Exception as e:
      # A metrics outage shouldn't stop the measurements
      log(f"warning: busy check failed, testing anyway: {e}")
      return False
    if any(v > check["limit_bps"] for v in values):
      return True
  return False


# Probe results are [metric name after the prefix, labels, value] samples
def speedtest(tools):
  r = tools.speedtest()
  samples = [
    ["speedtest_bits_per_second", {"direction": "down"}, r["download"]["bandwidth"] * 8],
    ["speedtest_bits_per_second", {"direction": "up"}, r["upload"]["bandwidth"] * 8],
    ["speedtest_latency_seconds", {}, r["ping"]["latency"] / 1000],
    ["speedtest_jitter_seconds", {}, r["ping"]["jitter"] / 1000],
    [
      "speedtest_info",
      {"server": f"{r['server']['name']} ({r['server']['location']})", "isp": r["isp"]},
      1,
    ],
  ]
  if "packetLoss" in r:
    samples.append(["speedtest_packet_loss_ratio", {}, r["packetLoss"] / 100])
  return samples


def iperf(tools, path, address):
  samples = [["iperf_up", {"path": path}, 1]]
  for direction, reverse in (("up", False), ("down", True)):
    result = tools.iperf(address, reverse)
    error = result.get("error")
    if error and "busy" in error:
      raise Skip("server_busy")
    if error and "unable to connect" in error:
      raise Skip("unreachable", [["iperf_up", {"path": path}, 0]])
    if error:
      raise RuntimeError(error)
    labels = {"path": path, "direction": direction}
    samples.append(
      ["iperf_bits_per_second", labels, result["end"]["sum_received"]["bits_per_second"]]
    )
    samples.append(["iperf_retransmits", labels, result["end"]["sum_sent"].get("retransmits", 0)])
  return samples


def finish(state, probe, now, samples=None, skipped=None):
  s = state.setdefault(probe, {"skipped": {}})
  s["attempt"] = now
  if samples is not None:
    s["samples"] = samples
  if skipped:
    s["skipped"][skipped] = s["skipped"].get(skipped, 0) + 1
    log(f"{probe}: skipped, {skipped}")
  else:
    s["success"] = now


def write(path, text):
  path.parent.mkdir(parents=True, exist_ok=True)
  tmp = path.with_suffix(".tmp")
  tmp.write_text(text)
  tmp.chmod(0o644)
  tmp.replace(path)


def exposition(config, state):
  isp = config["isp"]
  series = {
    "isp_plan_bits_per_second": [
      ({"direction": "down"}, isp["down_mbps"] * 1e6),
      ({"direction": "up"}, isp["up_mbps"] * 1e6),
    ]
  }
  for probe, s in sorted(state.items()):
    for name, labels, value in s.get("samples", []):
      series.setdefault(name, []).append((labels, value))
    series.setdefault("net_probe_last_attempt_timestamp_seconds", []).append(
      ({"probe": probe}, s["attempt"])
    )
    if "success" in s:
      series.setdefault("net_probe_last_success_timestamp_seconds", []).append(
        ({"probe": probe}, s["success"])
      )
    for reason, n in sorted(s["skipped"].items()):
      series.setdefault("net_probe_skipped_total", []).append(
        ({"probe": probe, "reason": reason}, n)
      )
  lines = []
  for name, (kind, text) in HELP.items():
    if name not in series:
      continue
    full = f"{config['metric_prefix']}_{name}"
    lines += [f"# HELP {full} {text}", f"# TYPE {full} {kind}"]
    for labels, value in series[name]:
      escaped = (v.replace("\\", "\\\\").replace('"', '\\"') for v in labels.values())
      label_text = ",".join(f'{k}="{v}"' for k, v in zip(labels, escaped))
      lines.append(full + ("{" + label_text + "}" if labels else "") + f" {value:.10g}")
  return "".join(f"{line}\n" for line in lines)


def run(config, now, tools):
  state_path, metrics_path = Path(config["state_path"]), Path(config["metrics_path"])
  state = json.loads(state_path.read_text()) if state_path.exists() else {}
  reachable = {}
  for path, address in config["targets"].items():
    loss, rtt = tools.ping(address)
    reachable[path] = loss < 1
    samples = [["ping_loss_ratio", {"path": path}, loss]]
    if rtt is not None:
      samples.append(["ping_rtt_seconds", {"path": path}, rtt])
    finish(state, f"ping_{path}", now, samples, None if reachable[path] else "unreachable")

  for test in config["tests"]:
    probe, path = test["probe"], test.get("path")
    if state.get(probe, {}).get("success", 0) >= latest_slot(now, test["slots"]):
      continue
    try:
      if path and not reachable[path]:
        raise Skip("unreachable", [["iperf_up", {"path": path}, 0]])
      if busy(tools, test["busy"]):
        raise Skip("busy")
      samples = iperf(tools, path, config["targets"][path]) if path else speedtest(tools)
    except Skip as e:
      finish(state, probe, now, e.samples, e.reason)
    except Exception as e:
      log(f"{probe}: {e!r}")
      finish(state, probe, now, None, "error")
    else:
      finish(state, probe, now, samples)
      log(f"{probe}: ok")

  write(state_path, json.dumps(state, indent=1))
  write(metrics_path, exposition(config, state))


def main():
  parser = argparse.ArgumentParser(
    description="Pings, speed tests and iperf3, into node_exporter's textfile collector."
  )
  parser.add_argument(
    "--config", default=DEFAULT_CONFIG, help=f"JSON config (default {DEFAULT_CONFIG})"
  )
  args = parser.parse_args()
  try:
    config = load_config(args.config)
  except ConfigError as e:
    sys.exit(f"net_probe: {e}")
  run(config, time.time(), Tools(config))


if __name__ == "__main__":
  main()
