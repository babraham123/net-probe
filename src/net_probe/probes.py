# The heavy tests and the busy check before each.

from .util import log


class Skip(Exception):
  """A probe that didn't measure: reason labels the skip, samples replace its last ones."""

  def __init__(self, reason, samples=None):
    super().__init__(reason)
    self.reason = reason
    self.samples = samples


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
